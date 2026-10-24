"""Background scheduler and publisher for Instagram Reels with retries and reminders."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
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
from utils import escape_html

log = logging.getLogger(__name__)

_SCHEDULER_LOCK = asyncio.Lock()
_SCHEDULER_TASK: asyncio.Task[None] | None = None
_LAST_TICK_TS: float = 0.0
_MEDIA_TOKENS: dict[str, Path] = {}


def register_media_token(file_path: Path) -> str:
    """Create a cryptographically random one-time token for serving a temporary media file."""
    token = secrets.token_urlsafe(32)
    _MEDIA_TOKENS[token] = file_path
    return token


def get_media_file(token: str, filename: str) -> Path | None:
    """Validate token and filename, returning the Path if valid."""
    if not token or not filename:
        return None
    path = _MEDIA_TOKENS.get(token)
    if path is None or path.name != filename:
        return None
    return path


def consume_media_token(token: str) -> Path | None:
    """Remove token from registry and delete the temporary media file."""
    path = _MEDIA_TOKENS.pop(token, None)
    if path is not None:
        path.unlink(missing_ok=True)
    return path


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
        # 1. Recover any stuck publishing posts (crash/timeout recovery)
        await _recover_stuck_publishing_posts(bot, now_utc)

        # 2. Check and prompt posts overdue by >= 6 hours
        await _check_overdue_posts(bot, now_utc)

        # 3. Daily token health check and auto-refresh
        await check_daily_token_refresh(bot)

        # 4. Atomically claim due posts (scheduled, <= now_utc, >= 6h ago, not locked)
        claimed = await db.claim_due_posts(now_utc)
        published_count = 0
        if claimed:
            log.info("claimed %d due post(s) for publishing", len(claimed))
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

    # Pre-check: ambiguous crash recovery (avoid re-publishing if already on Instagram)
    if post.caption:
        try:
            recovered = await client.check_recent_media_published(post.caption[:40])
            if recovered:
                media_id, permalink = recovered
                now_pub = datetime.now(timezone.utc)
                await db.update_post(
                    post_id,
                    status="published",
                    published_at=now_pub,
                    ig_media_id=media_id,
                    permalink=permalink,
                    locked_until=None,
                    error=None,
                )
                log.info("post %d already published on Instagram (pre-check recovered): %s", post_id, permalink)
                await _notify_owner(bot, post.owner_id, texts.IG_PUBLISHED.format(permalink=escape_html(permalink or "")))
                return True
        except Exception as exc:
            log.warning("error in pre-publish recovery check for post %d: %s", post_id, exc)

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
    media_token: str | None = None

    try:
        await bot.download(post.tg_file_id, destination=temp_video)
        if not temp_video.exists() or temp_video.stat().st_size == 0:
            raise ValueError("downloaded video file is empty")

        # Verify with ffprobe
        info = await probe(temp_video)
        if not info.has_video:
            raise ValueError("downloaded file has no video stream")

        # 3. Prepare caption with guaranteed hashtags
        caption = (post.caption or "").strip()
        if post.hashtags:
            existing_lower = caption.lower()
            missing_tags = [
                f"#{str(t).lstrip('#')}"
                for t in post.hashtags
                if str(t).strip() and f"#{str(t).lstrip('#').lower()}" not in existing_lower
            ]
            if missing_tags:
                caption = f"{caption}\n\n{' '.join(missing_tags)}".strip()

        # Check if Direct instagrapi mode is active
        if client._is_direct_mode():
            from services import ig_direct
            media_id, permalink = await ig_direct.upload_reel(temp_video, caption)
        else:
            # Official Meta Graph API container & upload flow
            file_size = temp_video.stat().st_size
            video_url: str | None = None

            if config.BASE_URL:
                media_token = register_media_token(temp_video)
                video_url = f"{config.BASE_URL.rstrip('/')}/media/{media_token}/{temp_video.name}"

            container_id = await client.create_reel_container(
                caption=caption,
                file_size=file_size,
                video_url=video_url,
                share_to_feed=True,
            )
            await db.update_post(post_id, ig_container_id=container_id)

            # 4. Stream Upload to rupload (when BASE_URL is not configured)
            if not video_url:
                await client.upload_video_resumable(container_id, temp_video)

            # 5. Poll Status
            await client.poll_container_status(container_id, timeout=600)
            if media_token:
                consume_media_token(media_token)
                media_token = None

            # 6. Publish with ambiguous recovery
            media_id = None
            permalink = None
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
            if not permalink:
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
        await _notify_owner(bot, post.owner_id, texts.IG_PUBLISHED.format(permalink=escape_html(permalink or "")))
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

    except (InstagramTransientError, asyncio.TimeoutError, aiohttp.ClientError, TelegramNetworkError) as exc:
        err = redact_token(str(exc))
        attempts = post.attempts + 1
        if attempts <= 3:
            backoff_mins = [1, 5, 15][attempts - 1]
            new_time = datetime.now(timezone.utc) + timedelta(minutes=backoff_mins)
            await db.update_post(post_id, status="scheduled", attempts=attempts,
                                 scheduled_at=new_time, error=err)
            log.warning("post %d transient network error (attempt %d/3): %s; retrying in %d min",
                        post_id, attempts, err, backoff_mins)
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
        if media_token:
            consume_media_token(media_token)
        temp_video.unlink(missing_ok=True)


async def _notify_owner(bot: Bot, owner_id: int, text: str) -> None:
    try:
        await bot.send_message(owner_id, text, disable_web_page_preview=False)
    except Exception as exc:
        log.warning("could not message owner %d: %s", owner_id, exc)


async def _notify_failure_with_buttons(bot: Bot, post: db.Post, reason: str) -> None:
    text = texts.IG_PUBLISH_FAILED.format(reason=escape_html(reason[:150]))
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
        caption_full = p.caption or ""
        if len(caption_full) > 400:
            preview = escape_html(caption_full[:395]) + "..."
        else:
            preview = escape_html(caption_full)
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
            views = metrics.get("views", metrics.get("plays", 0))
            reach = metrics.get("reach", 0)
            likes = metrics.get("likes", 0)
            comments = metrics.get("comments", 0)
            saved = metrics.get("saved", 0)

            msg = (
                f"📈 <b>Reel 24 soatlik statistikasi:</b>\n\n"
                f"🔗 {escape_html(p.permalink or '')}\n"
                f"👁 Ko'rishlar (views): {views:,}\n"
                f"👥 Qamrov (reach): {reach:,}\n"
                f"❤️ Layklar: {likes:,}\n"
                f"💬 Izohlar: {comments:,}\n"
                f"🔖 Saqlanganlar: {saved:,}"
            )
            await _notify_owner(bot, p.owner_id, msg)
        except Exception as exc:
            log.warning("could not send 24h stats for post %d: %s", p.id, exc)


async def _check_overdue_posts(bot: Bot, now_utc: datetime) -> int:
    """Find scheduled posts overdue by >= 6 hours, mark 'overdue', and prompt owner."""
    six_hours_ago = datetime.fromtimestamp(now_utc.timestamp() - 6 * 3600, tz=timezone.utc)
    overdue_posts = await db.get_overdue_scheduled_posts(six_hours_ago)
    if not overdue_posts:
        return 0

    count = 0
    for p in overdue_posts:
        await db.update_post(p.id, status="overdue", locked_until=None)
        preview = escape_html((p.caption or "")[:60])
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
            log.warning("could not notify overdue owner for post %d: %s", p.id, exc)
        count += 1

    return count


async def _recover_stuck_publishing_posts(
    bot: Bot, now_utc: datetime | None = None, client: InstagramClient | None = None
) -> int:
    """Recover posts left in 'publishing' status due to process crashes or expired locks."""
    stuck_posts = await db.get_stuck_publishing_posts(now_utc)
    if not stuck_posts:
        return 0

    log.warning("found %d post(s) stuck in 'publishing' status", len(stuck_posts))
    recovered_count = 0
    if client is None:
        token = await tokens.get_active_token()
        client = InstagramClient(token=token) if token else None
    ref_time = now_utc or datetime.now(timezone.utc)
    six_hours_ago = datetime.fromtimestamp(ref_time.timestamp() - 6 * 3600, tz=timezone.utc)

    for p in stuck_posts:
        recovered = None
        if client and p.caption:
            try:
                recovered = await client.check_recent_media_published(p.caption[:40])
            except Exception as exc:
                log.warning("error checking recent media for stuck post %d: %s", p.id, exc)

        if recovered:
            media_id, permalink = recovered
            await db.update_post(
                p.id,
                status="published",
                published_at=ref_time,
                ig_media_id=media_id,
                permalink=permalink,
                locked_until=None,
                error=None,
            )
            log.info("stuck post %d was already published: %s", p.id, permalink)
            await _notify_owner(bot, p.owner_id, texts.IG_PUBLISHED.format(permalink=escape_html(permalink or "")))
            recovered_count += 1
        elif p.attempts >= 3:
            err = "Publishing jarayoni to'xtab qolgan (crash/timeout)"
            await db.update_post(p.id, status="failed", locked_until=None, error=err)
            await _notify_failure_with_buttons(bot, p, err)
            recovered_count += 1
        elif p.scheduled_at and p.scheduled_at < six_hours_ago:
            # Overdue by >= 6 hours: mark overdue and ask owner
            await db.update_post(p.id, status="overdue", locked_until=None, error="Jarayon to'xtab qolgan (6+ soat o'tgan)")
            preview = escape_html((p.caption or "")[:60])
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
                log.warning("could not notify overdue owner for post %d: %s", p.id, exc)
            recovered_count += 1
        else:
            # Revert to scheduled for clean retry
            await db.update_post(p.id, status="scheduled", locked_until=None, attempts=p.attempts + 1)
            log.info("reset stuck post %d to 'scheduled' status (attempt %d)", p.id, p.attempts + 1)
            recovered_count += 1

    return recovered_count


async def check_daily_token_refresh(
    bot: Bot | None = None, force: bool = False, client: InstagramClient | None = None
) -> bool:
    """Daily background check for Instagram token health and auto-refresh."""
    now_ts = time.time()
    last_check_raw = await db.get_kv("last_daily_token_check_ts", 0.0)
    try:
        last_check = float(last_check_raw or 0.0)
    except (ValueError, TypeError):
        last_check = 0.0

    if not force and (now_ts - last_check) < 86400:
        return False  # Checked within last 24 hours

    await db.set_kv("last_daily_token_check_ts", now_ts)
    if client is None:
        token = await tokens.get_active_token()
        if not token:
            return False
        client = InstagramClient(token=token)
    refreshed = await tokens.maybe_refresh_token(client)
    if refreshed:
        log.info("daily check: Instagram token was successfully refreshed with Meta")
        if bot and config.OWNER_ID > 0:
            try:
                await bot.send_message(
                    config.OWNER_ID,
                    "🔄 <b>Instagram Token avtomatik yangilandi!</b>\n\n"
                    "Kunlik tekshiruv: Token muddati muvaffaqiyatli yana 60 kunga uzaytirildi."
                )
            except Exception as exc:
                log.debug("could not notify owner of token refresh: %s", exc)
        return True

    # Alert if token is expiring within 3 days and could not be refreshed
    status = await tokens.get_token_status()
    if status.get("configured") and status.get("days_left", 999) <= 3:
        if bot and config.OWNER_ID > 0:
            try:
                await bot.send_message(
                    config.OWNER_ID,
                    f"⚠️ <b>Diqqat: Instagram Token muddati tugamoqda!</b>\n\n"
                    f"Qolgan vaqt: {status.get('days_left')} kun.\n"
                    f"Iltimos, /igstatus orqali tekshiring yoki yangi token kiriting."
                )
            except Exception:
                pass
    return False


async def handle_startup_catchup(bot: Bot) -> None:
    """Startup catch-up: recover stuck publishing posts and prompt >=6h overdue posts."""
    now_utc = datetime.now(timezone.utc)
    await _recover_stuck_publishing_posts(bot, now_utc=None)
    await _check_overdue_posts(bot, now_utc)


async def _scheduler_loop(bot: Bot) -> None:
    """Main scheduler background loop running every SCHEDULER_INTERVAL_SEC."""
    log.info("Instagram scheduler background task started (interval: %ds)", config.SCHEDULER_INTERVAL_SEC)
    await handle_startup_catchup(bot)
    await check_daily_token_refresh(bot, force=False)

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
