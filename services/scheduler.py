"""Background scheduler and publisher for Instagram Reels with retries and reminders."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import texts
from services import db, tokens
from services.instagram import (
    InstagramClient,
    InstagramError,
    InstagramRateLimitError,
    InstagramTokenError,
    InstagramTransientError,
)
from services.probe import probe
from services.tokens import redact_token

log = logging.getLogger(__name__)

_SCHEDULER_LOCK = asyncio.Lock()
_SCHEDULER_TASK: asyncio.Task[None] | None = None
_LAST_TICK_TS: float = 0.0


def get_heartbeat_age_sec() -> float:
    """Return seconds since last scheduler tick."""
    if _LAST_TICK_TS == 0.0:
        return 0.0
    return time.time() - _LAST_TICK_TS


async def process_due_posts(bot: Bot) -> int:
    """Check and publish due posts atomically. Safe to call concurrently."""
    global _LAST_TICK_TS
    _LAST_TICK_TS = time.time()

    if _SCHEDULER_LOCK.locked():
        log.debug("scheduler tick skipped (already running)")
        return 0

    async with _SCHEDULER_LOCK:
        now_utc = datetime.now(timezone.utc)
        claimed = await db.claim_due_posts(now_utc)
        if not claimed:
            # Check reminders and stats
            await _check_reminders(bot, now_utc)
            await _check_stats_reports(bot, now_utc)
            return 0

        log.info("claimed %d due post(s) for publishing", len(claimed))
        published_count = 0
        for post in claimed:
            try:
                ok = await _publish_single_post(bot, post.id)
                if ok:
                    published_count += 1
            except Exception as exc:
                log.exception("unhandled error publishing post %d: %s", post.id, redact_token(str(exc)))

        # Also process reminders and 24h stats
        await _check_reminders(bot, now_utc)
        await _check_stats_reports(bot, now_utc)
        return published_count


async def _publish_single_post(bot: Bot, post_id: int) -> bool:
    """Execute the full download, upload, polling, and publishing pipeline for one post."""
    post = await db.get_post(post_id)
    if not post or post.status != "publishing":
        return False

    token = await tokens.get_active_token()
    if not token:
        err_msg = "Instagram token topilmadi yoki o'rnatilmagan"
        await db.update_post(post_id, status="failed", error=err_msg)
        await _notify_owner(bot, post.owner_id, f"{texts.IG_TOKEN_EXPIRED}\n\nPost #{post_id}")
        return False

    client = InstagramClient(token=token)
    await tokens.maybe_refresh_token(client)

    # 1. Content publishing limit check
    try:
        limit_data = await client.get_content_publishing_limit()
        config_data = limit_data.get("config", {})
        quota_total = config_data.get("quota_total", 50)
        quota_usage = limit_data.get("quota_usage", 0)
        if quota_usage >= quota_total > 0:
            new_time = datetime.now(timezone.utc) + timedelta(hours=2)
            await db.update_post(post_id, status="scheduled", scheduled_at=new_time,
                                 error="Instagram 24-soatlik post limiti tugadi")
            await _notify_owner(bot, post.owner_id, texts.IG_QUOTA_EXCEEDED)
            return False
    except InstagramTokenError:
        await db.update_post(post_id, status="scheduled",
                             scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
                             error="Token yaroqsiz")
        await _notify_owner(bot, post.owner_id, texts.IG_TOKEN_EXPIRED)
        return False
    except Exception as exc:
        log.warning("could not check publishing limit: %s", exc)

    # 2. Download stored video from Telegram
    if not post.tg_file_id:
        await db.update_post(post_id, status="failed", error="Telegram file_id mavjud emas")
        return False

    reels_temp = config.TEMP_ROOT / "reels"
    reels_temp.mkdir(parents=True, exist_ok=True)
    temp_video = reels_temp / f"reel_{post_id}_{int(time.time())}.mp4"

    try:
        await bot.download(post.tg_file_id, destination=temp_video)
        if not temp_video.exists() or temp_video.stat().st_size == 0:
            raise ValueError("downloaded video file is empty")

        # Verify with ffprobe
        info = await probe(temp_video)
        if not info.has_video:
            raise ValueError("downloaded file has no video stream")

        # 3. Create Reels Container
        caption = post.caption or ""
        file_size = temp_video.stat().st_size
        container_id = await client.create_reel_container(caption=caption, file_size=file_size, share_to_feed=True)
        await db.update_post(post_id, ig_container_id=container_id)

        # 4. Stream Upload to rupload
        await client.upload_video_resumable(container_id, temp_video)

        # 5. Poll Status
        await client.poll_container_status(container_id, timeout=600)

        # 6. Publish with ambiguous recovery
        media_id: str | None = None
        try:
            media_id = await client.publish_container(container_id)
        except (InstagramTransientError, asyncio.TimeoutError):
            log.warning("publish call timed out or failed; checking recent media")
            recovered = await client.check_recent_media_published(caption[:40])
            if recovered:
                media_id, permalink = recovered
                log.info("successfully recovered published post %s", media_id)

        if not media_id:
            raise InstagramError("media_publish failed to return media_id")

        # 7. Fetch Permalink
        permalink = await client.get_media_permalink(media_id)
        now_pub = datetime.now(timezone.utc)
        await db.update_post(
            post_id,
            status="published",
            published_at=now_pub,
            ig_media_id=media_id,
            permalink=permalink,
            error=None,
        )
        log.info("post %d successfully published: %s", post_id, permalink)

        # Notify Owner
        await _notify_owner(bot, post.owner_id, texts.IG_PUBLISHED.format(permalink=permalink))
        return True

    except InstagramTokenError as exc:
        err = redact_token(str(exc))
        await db.update_post(post_id, status="scheduled",
                             scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
                             error=err)
        await _notify_owner(bot, post.owner_id, f"{texts.IG_TOKEN_EXPIRED}\n\n{err}")
        return False

    except InstagramRateLimitError as exc:
        err = redact_token(str(exc))
        new_time = datetime.now(timezone.utc) + timedelta(hours=2)
        await db.update_post(post_id, status="scheduled", scheduled_at=new_time, error=err)
        await _notify_owner(bot, post.owner_id, texts.IG_QUOTA_EXCEEDED)
        return False

    except (InstagramTransientError, asyncio.TimeoutError) as exc:
        err = redact_token(str(exc))
        attempts = post.attempts + 1
        if attempts <= 3:
            backoff_mins = [1, 5, 15][attempts - 1]
            new_time = datetime.now(timezone.utc) + timedelta(minutes=backoff_mins)
            await db.update_post(post_id, status="scheduled", attempts=attempts,
                                 scheduled_at=new_time, error=err)
            log.warning("post %d transient error (attempt %d/3); retrying in %d min",
                        post_id, attempts, backoff_mins)
        else:
            await db.update_post(post_id, status="failed", attempts=attempts, error=err)
            await _notify_failure_with_buttons(bot, post, f"3 ta urinishdan so'ng xatolik: {err}")
        return False

    except Exception as exc:
        err = redact_token(str(exc))
        log.error("permanent failure publishing post %d: %s", post_id, err)
        await db.update_post(post_id, status="failed", error=err)
        await _notify_failure_with_buttons(bot, post, err)
        return False

    finally:
        temp_video.unlink(missing_ok=True)


async def _notify_owner(bot: Bot, owner_id: int, text: str) -> None:
    try:
        await bot.send_message(owner_id, text, disable_web_page_preview=False)
    except Exception as exc:
        log.warning("could not message owner %d: %s", owner_id, exc)


async def _notify_failure_with_buttons(bot: Bot, post: db.Post, reason: str) -> None:
    text = texts.IG_PUBLISH_FAILED.format(reason=reason[:150])
    buttons = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Qayta urinish", callback_data=f"ig_retry_{post.id}"),
            InlineKeyboardButton(text="⏰ Vaqtni o'zgartirish", callback_data=f"ig_reschedule_{post.id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Bekor qilish", callback_data=f"ig_cancel_{post.id}"),
        ]
    ])
    try:
        await bot.send_message(post.owner_id, text, reply_markup=buttons)
    except Exception as exc:
        log.warning("could not send failure notification: %s", exc)


async def _check_reminders(bot: Bot, now_utc: datetime) -> None:
    """Send reminder 10 minutes prior to scheduled publication."""
    window_start = now_utc
    window_end = now_utc + timedelta(minutes=12)

    async with db.session_scope() as session:
        from sqlalchemy import select
        stmt = (
            select(db.Post)
            .where(
                (db.Post.status == "scheduled")
                & (db.Post.reminder_sent == False)  # noqa: E712
                & (db.Post.scheduled_at >= window_start)
                & (db.Post.scheduled_at <= window_end)
            )
        )
        res = await session.execute(stmt)
        posts_to_remind = list(res.scalars().all())
        for p in posts_to_remind:
            p.reminder_sent = True

    for p in posts_to_remind:
        preview = (p.caption or "")[:60]
        text = texts.IG_REMINDER_10MIN.format(preview=preview)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"ig_cancel_{p.id}")
        ]])
        try:
            await bot.send_message(p.owner_id, text, reply_markup=kb)
        except Exception as exc:
            log.debug("failed to send 10-min reminder: %s", exc)


async def _check_stats_reports(bot: Bot, now_utc: datetime) -> None:
    """Send engagement statistics ~24 hours after publication."""
    window = now_utc - timedelta(hours=24)

    async with db.session_scope() as session:
        from sqlalchemy import select
        stmt = (
            select(db.Post)
            .where(
                (db.Post.status == "published")
                & (db.Post.stats_sent == False)  # noqa: E712
                & (db.Post.published_at <= window)
                & (db.Post.ig_media_id != None)  # noqa: E711
            )
            .limit(5)
        )
        res = await session.execute(stmt)
        posts_for_stats = list(res.scalars().all())
        for p in posts_for_stats:
            p.stats_sent = True

    if not posts_for_stats:
        return

    token = await tokens.get_active_token()
    if not token:
        return
    client = InstagramClient(token=token)

    for p in posts_for_stats:
        try:
            metrics = await client.get_media_insights(p.ig_media_id)  # type: ignore[arg-type]
            plays = metrics.get("plays", 0)
            reach = metrics.get("reach", 0)
            likes = metrics.get("likes", 0)
            comments = metrics.get("comments", 0)
            saved = metrics.get("saved", 0)

            msg = (
                f"📈 <b>Reel 24 soatlik statistikasi:</b>\n\n"
                f"🔗 {p.permalink}\n"
                f"👁 Ko'rishlar (plays): {plays:,}\n"
                f"👥 Qamrov (reach): {reach:,}\n"
                f"❤️ Layklar: {likes:,}\n"
                f"💬 Izohlar: {comments:,}\n"
                f"🔖 Saqlanganlar: {saved:,}"
            )
            await _notify_owner(bot, p.owner_id, msg)
        except Exception as exc:
            log.warning("could not send 24h stats for post %d: %s", p.id, exc)


async def handle_startup_catchup(bot: Bot) -> None:
    """Catch-up logic on startup: publish overdue (<6h) or ask owner (>6h)."""
    now_utc = datetime.now(timezone.utc)
    six_hours_ago = now_utc - timedelta(hours=6)

    async with db.session_scope() as session:
        from sqlalchemy import select
        stmt = (
            select(db.Post)
            .where(
                (db.Post.status == "scheduled")
                & (db.Post.scheduled_at < now_utc)
            )
        )
        res = await session.execute(stmt)
        overdue_posts = list(res.scalars().all())

    for p in overdue_posts:
        if p.scheduled_at and p.scheduled_at < six_hours_ago:
            # Overdue by > 6 hours: ask owner instead of blind publishing
            preview = (p.caption or "")[:60]
            text = texts.IG_OVERDUE_ASK.format(preview=preview)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Hozir joylash", callback_data=f"ig_post_now_{p.id}"),
                    InlineKeyboardButton(text="⏰ Vaqtni o'zgartirish", callback_data=f"ig_reschedule_{p.id}"),
                ],
                [
                    InlineKeyboardButton(text="🗑 Bekor qilish", callback_data=f"ig_cancel_{p.id}"),
                ]
            ])
            try:
                await bot.send_message(p.owner_id, text, reply_markup=kb)
            except Exception as exc:
                log.warning("could not notify overdue post %d: %s", p.id, exc)


async def _scheduler_loop(bot: Bot) -> None:
    """Main scheduler background loop running every SCHEDULER_INTERVAL_SEC."""
    log.info("Instagram scheduler background task started (interval: %ds)", config.SCHEDULER_INTERVAL_SEC)
    await handle_startup_catchup(bot)

    while True:
        try:
            await process_due_posts(bot)
        except Exception as exc:
            log.exception("unexpected error in scheduler loop: %s", redact_token(str(exc)))
        await asyncio.sleep(config.SCHEDULER_INTERVAL_SEC)


def start_scheduler(bot: Bot) -> asyncio.Task[None]:
    global _SCHEDULER_TASK
    if _SCHEDULER_TASK is None or _SCHEDULER_TASK.done():
        _SCHEDULER_TASK = asyncio.create_task(_scheduler_loop(bot))
    return _SCHEDULER_TASK


def stop_scheduler() -> None:
    global _SCHEDULER_TASK
    if _SCHEDULER_TASK is not None:
        _SCHEDULER_TASK.cancel()
        _SCHEDULER_TASK = None
        log.info("Instagram scheduler stopped")
