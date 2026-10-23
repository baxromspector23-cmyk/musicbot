"""Instagram Reels auto-poster handlers: intake, AI captioning, scheduling, and post management."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

import config
import keyboards as kb
import texts
from keyboards import MenuCb
from services import besttime, db, intake, llm, scheduler, session as store, tokens, video_prep
from services.instagram import InstagramClient
from states import IgStates
from utils import UserError, escape_html, fmt_duration, progress_bar

log = logging.getLogger(__name__)

router = Router(name="instagram")


# ----------------------------------------------------------------------
# Bot Commands
# ----------------------------------------------------------------------

@router.message(Command("reel"))
async def cmd_reel(message: Message, state: FSMContext) -> None:
    """Start the Instagram Reel intake flow."""
    await state.clear()
    await state.set_state(IgStates.waiting_video)
    await message.answer(texts.REEL_ASK_VIDEO)


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    """List currently scheduled Instagram posts with management buttons."""
    posts = await db.list_scheduled_posts(limit=20)
    if not posts:
        await message.answer(texts.QUEUE_EMPTY)
        return

    tz = ZoneInfo(config.TZ)
    lines = [texts.QUEUE_TITLE]
    for p in posts:
        loc_time = p.scheduled_at.astimezone(tz).strftime("%d.%m %H:%M") if p.scheduled_at else "—"
        cap = escape_html((p.caption or "")[:40].replace("\n", " "))
        lines.append(f"• <b>#{p.id}</b> | 📅 {loc_time} | 📝 {cap}...")

    await message.answer("\n".join(lines))
    # Send management cards for up to 3 upcoming posts
    for p in posts[:3]:
        loc_time = p.scheduled_at.astimezone(tz).strftime("%d.%m %H:%M") if p.scheduled_at else "—"
        cap = escape_html((p.caption or "")[:50])
        text = f"Post <b>#{p.id}</b> ({loc_time}):\n<i>{cap}</i>"
        await message.answer(text, reply_markup=kb.post_manage_kb(p.id))


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    """List last 10 published posts with permalinks."""
    posts = await db.list_published_posts(limit=10)
    if not posts:
        await message.answer(texts.HISTORY_EMPTY)
        return

    tz = ZoneInfo(config.TZ)
    lines = [texts.HISTORY_TITLE]
    for p in posts:
        loc_time = p.published_at.astimezone(tz).strftime("%d.%m %H:%M") if p.published_at else "—"
        cap = escape_html((p.caption or "")[:35].replace("\n", " "))
        link = escape_html(p.permalink or "—")
        lines.append(f"• {loc_time} | <a href='{link}'>{cap}</a>")

    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Show insights for recent published posts."""
    posts = await db.list_published_posts(limit=5)
    if not posts:
        await message.answer(texts.STATS_EMPTY)
        return

    token = await tokens.get_active_token()
    if not token:
        await message.answer(texts.IG_TOKEN_EXPIRED)
        return

    client = InstagramClient(token=token)
    msg_lines = [texts.STATS_TITLE]

    for p in posts:
        if not p.ig_media_id:
            continue
        try:
            m = await client.get_media_insights(p.ig_media_id)
            plays = m.get("plays", 0)
            reach = m.get("reach", 0)
            likes = m.get("likes", 0)
            comments = m.get("comments", 0)
            cap = escape_html((p.caption or "")[:30].replace("\n", " "))
            link = escape_html(p.permalink or "")
            msg_lines.append(
                f"🎬 <a href='{link}'>{cap}</a>\n"
                f"👁 {plays:,} plays | 👥 {reach:,} reach | ❤️ {likes:,} | 💬 {comments:,}\n"
            )
        except Exception:
            continue

    if len(msg_lines) <= 1:
        await message.answer(texts.STATS_EMPTY)
    else:
        await message.answer("\n".join(msg_lines), disable_web_page_preview=True)


@router.message(Command("igstatus"))
async def cmd_igstatus(message: Message) -> None:
    """Display Instagram account info, token status, and insights availability."""
    token = await tokens.get_active_token()
    tok_info = await tokens.get_token_status()
    queue = await db.list_scheduled_posts(limit=100)

    username = "Noma'lum"
    user_id = config.IG_USER_ID or "o'rnatilmagan"
    insights_status = "mavjud emas"
    last_res = "muvaffaqiyatli"

    if token:
        client = InstagramClient(token=token)
        try:
            acc = await client.get_account_info()
            username = acc.get("username", username)
            user_id = acc.get("id", user_id)
        except Exception as exc:
            username = f"xatolik ({tokens.redact_token(str(exc))[:30]})"

        try:
            inf = await client.get_online_followers()
            if inf:
                insights_status = "faol (auditoriya tahlili ishlamoqda)"
            else:
                insights_status = "cheklangan (<100 obunachi yoki ruxsat yo'q)"
        except Exception:
            insights_status = "mavjud emas"

    # Check last published or failed post
    last_pub = await db.list_published_posts(limit=1)
    if last_pub:
        last_res = f"Post #{last_pub[0].id} joylandi"

    text = texts.IGSTATUS_INFO.format(
        username=escape_html(username),
        user_id=escape_html(str(user_id)),
        days_left=tok_info.get("days_left", 0),
        token_status=escape_html(str(tok_info.get("status", "noma'lum"))),
        insights_status=escape_html(insights_status),
        queue_count=len(queue),
        last_result=escape_html(last_res),
    )
    await message.answer(text)


# ----------------------------------------------------------------------
# Entry via Main Menu ("Instagramga joylash")
# ----------------------------------------------------------------------

@router.callback_query(MenuCb.filter(F.action == "ig_reel"))
async def on_menu_ig_reel(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    """Start Reel publishing from an existing video editing session."""
    s = store.get_valid(cb.from_user.id, callback_data.sid)
    if s is None:
        await cb.answer(texts.SESSION_EXPIRED, show_alert=True)
        return
    if not s.info.has_video:
        await cb.answer(texts.REEL_NO_VIDEO, show_alert=True)
        return

    await cb.answer()
    video_path = s.current
    await state.update_data(is_session_file=True)
    await _start_video_intake(bot, cb.from_user.id, video_path, s.info, state)


# ----------------------------------------------------------------------
# Video Intake & Aspect Ratio Selection
# ----------------------------------------------------------------------

@router.message(IgStates.waiting_video, F.video | F.document)
async def on_video_message(message: Message, state: FSMContext, bot: Bot) -> None:
    """Receive video file for Instagram Reel."""
    inc = intake.extract_incoming(message)
    if inc is None:
        await message.answer(texts.REEL_NO_VIDEO)
        return
    try:
        intake.precheck_size(inc)
    except UserError as exc:
        await message.answer(escape_html(str(exc)))
        return

    status = await message.answer(texts.DOWNLOADING)
    temp_dir = config.TEMP_ROOT / "temp_reels" / str(message.from_user.id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"intake_{inc.stem}.{inc.ext}"

    try:
        info = await intake.fetch(bot, inc, temp_file)
    except UserError as exc:
        temp_file.unlink(missing_ok=True)
        await status.edit_text(escape_html(str(exc)))
        return
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise

    await status.delete()
    await state.update_data(is_session_file=False)
    await _start_video_intake(bot, message.chat.id, temp_file, info, state)


async def _start_video_intake(
    bot: Bot,
    chat_id: int,
    video_path: Path,
    info: intake.MediaInfo,
    state: FSMContext,
) -> None:
    try:
        info, specs = await video_prep.check_video_specs(video_path)
    except UserError as exc:
        await bot.send_message(chat_id, escape_html(str(exc)))
        return

    await state.update_data(
        raw_video_path=str(video_path),
        width=specs.width,
        height=specs.height,
        duration=specs.duration,
        has_audio=info.has_audio,
        title=info.title,
        artist=info.artist,
    )

    if specs.is_9_16:
        # Already 9:16 aspect ratio: proceed with original
        await _process_normalization(bot, chat_id, "original", state)
    else:
        # Ask aspect ratio choice
        await state.set_state(IgStates.waiting_aspect)
        prompt = texts.REEL_ASPECT_CHOOSE.format(w=specs.width, h=specs.height)
        await bot.send_message(chat_id, prompt, reply_markup=kb.reel_aspect_kb())


@router.callback_query(IgStates.waiting_aspect, F.data.startswith("ig_asp_"))
async def on_aspect_chosen(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Handle aspect ratio selection."""
    await cb.answer()
    mode = cb.data.replace("ig_asp_", "")  # "crop", "pad", "orig"
    aspect_mode = "original" if mode == "orig" else mode
    await cb.message.delete()
    await _process_normalization(bot, cb.from_user.id, aspect_mode, state)


async def _process_normalization(
    bot: Bot,
    chat_id: int,
    aspect_mode: str,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    raw_path = Path(data["raw_video_path"])
    duration = data.get("duration", 10.0)

    status_msg = await bot.send_message(chat_id, f"{texts.REEL_PREPARING}\n{progress_bar(0)}")

    async def on_progress(pct: float) -> None:
        try:
            await status_msg.edit_text(f"{texts.REEL_PREPARING}\n{progress_bar(pct)}")
        except Exception:
            pass

    out_file = raw_path.parent / f"norm_{raw_path.stem}.mp4"

    try:
        from services.video_prep import VideoSpecs
        specs = VideoSpecs(
            width=data.get("width", 1080),
            height=data.get("height", 1920),
            duration=duration,
            fps=30.0,
            is_9_16=True,
            codec_name="h264",
            size=raw_path.stat().st_size,
        )
        dummy_info = intake.MediaInfo(duration=duration, has_audio=data.get("has_audio", True))
        await video_prep.prepare_reel_video(
            raw_path, out_file, dummy_info, specs, aspect_mode=aspect_mode, on_progress=on_progress
        )
    except UserError as exc:
        await status_msg.edit_text(escape_html(str(exc)))
        return
    except Exception as exc:
        await status_msg.edit_text(texts.FFMPEG_FAIL)
        log.exception("normalization failed: %s", exc)
        return

    # Extract 3 frames for AI captioning
    frames = await video_prep.extract_preview_frames(out_file, duration)

    # Persist the video durably into Telegram storage chat (or owner chat)
    storage_chat = config.STORAGE_CHAT_ID or chat_id
    try:
        sent_doc = await bot.send_document(
            storage_chat,
            document=FSInputFile(out_file, filename="reel_ready.mp4"),
            caption="📦 Instagram Reels Storage Video",
        )
        tg_file_id = sent_doc.document.file_id  # type: ignore[union-attr]
        tg_unique_id = sent_doc.document.file_unique_id  # type: ignore[union-attr]
        storage_msg_id = sent_doc.message_id
    except Exception as exc:
        log.warning("could not store video to chat %s: %s", storage_chat, exc)
        await status_msg.edit_text("Videoni saqlashda xatolik yuz berdi. Qayta urinib ko'ring.")
        return
    finally:
        out_file.unlink(missing_ok=True)
        if not data.get("is_session_file"):
            raw_path.unlink(missing_ok=True)
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Create Post record in DB as draft
    post = await db.create_post(
        owner_id=chat_id,
        storage_chat_id=storage_chat,
        storage_message_id=storage_msg_id,
        tg_file_id=tg_file_id,
        file_unique_id=tg_unique_id,
        file_size=sent_doc.document.file_size,  # type: ignore[union-attr]
        duration=duration,
        width=1080 if aspect_mode != "original" else data.get("width"),
        height=1920 if aspect_mode != "original" else data.get("height"),
        status="draft",
    )

    # Save post_id and extracted frames in state
    import base64
    frames_b64 = [base64.b64encode(f).decode("ascii") for f in frames]
    await state.update_data(post_id=post.id, frames_b64=frames_b64)
    await state.set_state(IgStates.waiting_description)
    await bot.send_message(chat_id, texts.REEL_ASK_DESC)


# ----------------------------------------------------------------------
# Description Intake & AI Caption Generation
# ----------------------------------------------------------------------

@router.message(IgStates.waiting_description, F.text)
async def on_description_entered(message: Message, state: FSMContext, bot: Bot) -> None:
    """Generate AI caption from owner's description and extracted frames."""
    data = await state.get_data()
    post_id = data.get("post_id")
    if not post_id:
        await state.clear()
        await message.answer(texts.SESSION_EXPIRED)
        return

    desc_text = message.text or ""
    import base64
    frames = [base64.b64decode(b) for b in data.get("frames_b64", [])]
    audio_tags = {"title": data.get("title", ""), "artist": data.get("artist", "")}

    status = await message.answer(texts.REEL_AI_WORKING)

    ai_res = await llm.generate_caption(
        description=desc_text,
        audio_tags=audio_tags,
        frames=frames,
    )

    try:
        await status.delete()
    except Exception:
        pass

    caption = ai_res["caption"]
    hashtags = ai_res["hashtags"]
    full_text = ai_res["full_text"]

    await db.update_post(post_id, caption=full_text, hashtags=hashtags)
    await state.update_data(last_desc=desc_text)

    preview_text = texts.REEL_PREVIEW_CARD.format(
        caption=escape_html(caption),
        hashtags=escape_html(" ".join("#" + t for t in hashtags)),
    )
    if ai_res.get("is_fallback"):
        preview_text += f"\n\n⚠️ <i>{texts.REEL_AI_FALLBACK}</i>"

    await message.answer(preview_text, reply_markup=kb.reel_preview_kb(post_id))


# ----------------------------------------------------------------------
# Caption Preview & Editing Callbacks
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ig_rewrite_"))
async def on_rewrite_caption(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Regenerate caption with LLM."""
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    data = await state.get_data()
    desc_text = data.get("last_desc", "Video")

    import base64
    frames = [base64.b64decode(b) for b in data.get("frames_b64", [])]
    audio_tags = {"title": data.get("title", ""), "artist": data.get("artist", "")}

    status = await cb.message.answer(texts.REEL_AI_WORKING)
    ai_res = await llm.generate_caption(desc_text, audio_tags, frames)
    try:
        await status.delete()
    except Exception:
        pass

    await db.update_post(post_id, caption=ai_res["full_text"], hashtags=ai_res["hashtags"])

    preview_text = texts.REEL_PREVIEW_CARD.format(
        caption=escape_html(ai_res["caption"]),
        hashtags=escape_html(" ".join("#" + t for t in ai_res["hashtags"])),
    )
    await cb.message.edit_text(preview_text, reply_markup=kb.reel_preview_kb(post_id))


@router.callback_query(F.data.startswith("ig_edit_cap_"))
async def on_edit_caption_click(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    await state.update_data(editing_post_id=post_id)
    await state.set_state(IgStates.editing_caption)
    await cb.message.answer(texts.REEL_EDIT_CAPTION_PROMPT)


@router.message(IgStates.editing_caption, F.text)
async def on_caption_edited(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("editing_post_id") or data.get("post_id")
    if not post_id:
        await state.clear()
        return

    post = await db.get_post(post_id)
    if not post:
        await state.clear()
        return

    raw_input = (message.text or "")[:2200].strip()
    clean_body, caption_tags = llm.extract_caption_body_and_tags(raw_input)

    # If user included hashtags in their edited caption, prioritize them;
    # otherwise retain existing hashtags
    tags_source = caption_tags if caption_tags else (post.hashtags or [])
    clean_tags = llm.validate_and_format_hashtags(
        tags_source, config.BRAND_HASHTAG, limit=config.HASHTAG_LIMIT
    )

    full_text = f"{clean_body}\n\n{' '.join('#' + t for t in clean_tags)}".strip() if clean_tags else clean_body
    await db.update_post(post_id, caption=full_text, hashtags=clean_tags)

    preview_text = texts.REEL_PREVIEW_CARD.format(
        caption=escape_html(clean_body),
        hashtags=escape_html(" ".join("#" + t for t in clean_tags)),
    )
    await message.answer(preview_text, reply_markup=kb.reel_preview_kb(post_id))


@router.callback_query(F.data.startswith("ig_edit_tags_"))
async def on_edit_tags_click(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    await state.update_data(editing_post_id=post_id)
    await state.set_state(IgStates.editing_hashtags)
    prompt = texts.REEL_EDIT_HASHTAGS_PROMPT.format(limit=config.HASHTAG_LIMIT)
    await cb.message.answer(prompt)


@router.message(IgStates.editing_hashtags, F.text)
async def on_hashtags_edited(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("editing_post_id") or data.get("post_id")
    if not post_id:
        await state.clear()
        return

    post = await db.get_post(post_id)
    if not post:
        await state.clear()
        return

    raw_tags = llm.parse_raw_hashtags(message.text or "")
    clean_tags = llm.validate_and_format_hashtags(raw_tags, config.BRAND_HASHTAG, limit=config.HASHTAG_LIMIT)

    # Safely extract body while preserving all paragraphs intact
    body, _ = llm.extract_caption_body_and_tags(post.caption or "")
    full_text = f"{body}\n\n{' '.join('#' + t for t in clean_tags)}".strip() if clean_tags else body
    await db.update_post(post_id, caption=full_text, hashtags=clean_tags)

    preview_text = texts.REEL_PREVIEW_CARD.format(
        caption=escape_html(body),
        hashtags=escape_html(" ".join("#" + t for t in clean_tags)),
    )
    await message.answer(preview_text, reply_markup=kb.reel_preview_kb(post_id))


# ----------------------------------------------------------------------
# Scheduling & Best-Time Engine Flow
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ig_confirm_"))
async def on_confirm_caption(cb: CallbackQuery, state: FSMContext) -> None:
    """Move to time selection."""
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])

    token = await tokens.get_active_token()
    client = InstagramClient(token=token) if token else None

    # Get explanation of best time
    _, explanation = await besttime.pick_best_slot(client)
    await cb.message.edit_text(
        texts.REEL_TIME_CHOOSE.format(explanation=escape_html(explanation)),
        reply_markup=kb.reel_time_kb(post_id),
    )


@router.callback_query(F.data.startswith("ig_time_auto_"))
async def on_time_auto(cb: CallbackQuery, state: FSMContext) -> None:
    """Automatically schedule at the best calculated slot."""
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])

    token = await tokens.get_active_token()
    client = InstagramClient(token=token) if token else None
    slot_utc, _ = await besttime.pick_best_slot(client)

    await _finalize_scheduling(cb.message, post_id, slot_utc, state)


@router.callback_query(F.data.startswith("ig_time_now_"))
async def on_time_now(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Schedule for immediate publication."""
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    now_utc = datetime.now(timezone.utc)

    await _finalize_scheduling(cb.message, post_id, now_utc, state)
    # Trigger publisher immediately
    asyncio.create_task(scheduler.process_due_posts(bot))


@router.callback_query(F.data.startswith("ig_time_manual_"))
async def on_time_manual(cb: CallbackQuery, state: FSMContext) -> None:
    """Ask owner for manual schedule time."""
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    await state.update_data(scheduling_post_id=post_id)
    await state.set_state(IgStates.waiting_time)
    await cb.message.answer(texts.REEL_TIME_MANUAL_PROMPT)


@router.message(IgStates.waiting_time, F.text)
@router.message(IgStates.editing_time, F.text)
async def on_manual_time_entered(message: Message, state: FSMContext) -> None:
    """Parse and apply typed schedule time."""
    data = await state.get_data()
    post_id = data.get("scheduling_post_id") or data.get("reschedule_post_id")
    if not post_id:
        await state.clear()
        return

    try:
        target_utc = besttime.parse_scheduled_time(message.text or "")
    except ValueError:
        await message.answer(texts.REEL_TIME_BAD)
        return

    await _finalize_scheduling(message, post_id, target_utc, state)


async def _finalize_scheduling(
    msg: Message,
    post_id: int,
    scheduled_utc: datetime,
    state: FSMContext,
) -> None:
    """Save scheduled time, set status='scheduled', send confirmation card."""
    await state.clear()
    post = await db.update_post(post_id, scheduled_at=scheduled_utc, status="scheduled", error=None)
    if not post:
        return

    tz = ZoneInfo(config.TZ)
    local_dt = scheduled_utc.astimezone(tz)
    local_str = local_dt.strftime("%d.%m.%Y %H:%M")

    # Determine queue position
    scheduled_posts = await db.list_scheduled_posts(limit=100)
    pos = 1
    for idx, p in enumerate(scheduled_posts, 1):
        if p.id == post_id:
            pos = idx
            break

    preview = escape_html((post.caption or "")[:80])
    confirm_text = texts.REEL_SCHEDULED_CARD.format(
        local_time=local_str,
        tz=config.TZ,
        position=pos,
        preview=preview,
    )
    await msg.answer(confirm_text)


# ----------------------------------------------------------------------
# Post Management Callbacks (/queue, reminders, failure alerts)
# ----------------------------------------------------------------------

@router.callback_query(F.data.startswith("ig_reschedule_"))
async def on_reschedule_click(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    await state.update_data(reschedule_post_id=post_id)
    await state.set_state(IgStates.editing_time)
    await cb.message.answer(texts.REEL_TIME_MANUAL_PROMPT)


@router.callback_query(F.data.startswith("ig_post_now_"))
async def on_post_now_click(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    now_utc = datetime.now(timezone.utc)
    await db.update_post(post_id, scheduled_at=now_utc, status="scheduled")
    await cb.message.answer(f"Post #{post_id} hozir joylashga yuborildi.")
    asyncio.create_task(scheduler.process_due_posts(bot))


@router.callback_query(F.data.startswith("ig_retry_"))
async def on_retry_click(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    post_id = int(cb.data.split("_")[-1])
    now_utc = datetime.now(timezone.utc)
    await db.update_post(post_id, scheduled_at=now_utc, status="scheduled", attempts=0, error=None)
    await cb.message.answer(f"Post #{post_id} qayta urinishga qo'yildi.")
    asyncio.create_task(scheduler.process_due_posts(bot))


@router.callback_query(F.data == "ig_cancel_flow")
async def on_cancel_flow(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    if post_id:
        try:
            post = await db.get_post(post_id)
            if post and post.status == "draft":
                await db.delete_post(post_id)
        except Exception:
            pass
    await state.clear()
    await cb.answer()
    await cb.message.answer(texts.CANCELLED)


@router.callback_query(F.data.regexp(r"^ig_cancel_(\d+)$"))
async def on_cancel_post_click(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        post_id = int(cb.data.split("_")[-1])
        post = await db.get_post(post_id)
        if not post:
            await cb.message.answer(texts.CANCELLED)
            return
        if post.status == "published":
            await cb.message.answer("Bu post allaqachon Instagram'ga joylangan. Uni tarixdan o'chirib bo'lmaydi.")
            return
        await db.delete_post(post_id)
        await cb.message.answer(f"Post #{post_id} bekor qilindi va navbatdan o'chirildi.")
    except Exception:
        await cb.message.answer(texts.CANCELLED)
