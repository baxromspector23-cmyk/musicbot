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
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
    failed_posts = await db.list_failed_posts(limit=5)

    if not posts and not failed_posts:
        await message.answer(texts.QUEUE_EMPTY)
        return

    tz = ZoneInfo(config.TZ)
    lines = []
    if posts:
        lines.append(texts.QUEUE_TITLE)
        for p in posts:
            loc_time = p.scheduled_at.astimezone(tz).strftime("%d.%m %H:%M") if p.scheduled_at else "—"
            cap = escape_html((p.caption or "")[:40].replace("\n", " "))
            tag = "⚠️ Vaqti o'tgan" if p.status == "overdue" else f"📅 {loc_time}"
            lines.append(f"• <b>#{p.id}</b> | {tag} | 📝 {cap}...")

    if failed_posts:
        lines.append("\n⚠️ <b>Xatolik tufayli to'xtagan postlar:</b>")
        for fp in failed_posts:
            err = escape_html((fp.error or "Noma'lum xatolik")[:50])
            cap = escape_html((fp.caption or "")[:35].replace("\n", " "))
            lines.append(f"• <b>#{fp.id}</b> | ❌ <i>{err}</i>\n  📝 {cap}...")

    await message.answer("\n".join(lines))

    # Send management cards for up to 3 upcoming scheduled posts
    for p in posts[:3]:
        loc_time = p.scheduled_at.astimezone(tz).strftime("%d.%m %H:%M") if p.scheduled_at else "—"
        status_note = " (⚠️ Vaqti o'tgan)" if p.status == "overdue" else ""
        cap = escape_html((p.caption or "")[:50])
        text = f"Post <b>#{p.id}</b> ({loc_time}){status_note}:\n<i>{cap}</i>"
        await message.answer(text, reply_markup=kb.post_manage_kb(p.id))

    # Send retry cards for failed posts
    for fp in failed_posts[:2]:
        cap = escape_html((fp.caption or "")[:50])
        err = escape_html((fp.error or "")[:60])
        text = f"⚠️ Post <b>#{fp.id}</b> (Xatolik: {err}):\n<i>{cap}</i>"
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Qayta urinish", callback_data=f"ig_retry_{fp.id}"),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"ig_cancel_{fp.id}"),
        ]])
        await message.answer(text, reply_markup=retry_kb)


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
        failed = await db.list_failed_posts(limit=1)
        scheduled = await db.list_scheduled_posts(limit=1)
        extra = ""
        if scheduled:
            extra += "\n\n📅 Hozirda rejalashtirilgan postlar mavjud: /queue"
        if failed:
            extra += "\n\n⚠️ Xatolik tufayli to'xtagan post mavjud: /queue orqali ko'rishingiz va qayta urinishingiz mumkin."
        await message.answer(texts.STATS_EMPTY + extra)
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
            views = m.get("views", m.get("plays", 0))
            reach = m.get("reach", 0)
            likes = m.get("likes", 0)
            comments = m.get("comments", 0)
            cap = escape_html((p.caption or "")[:30].replace("\n", " "))
            link = escape_html(p.permalink or "")
            msg_lines.append(
                f"🎬 <a href='{link}'>{cap}</a>\n"
                f"👁 {views:,} views | 👥 {reach:,} reach | ❤️ {likes:,} | 💬 {comments:,}\n"
            )
        except Exception:
            continue

    if len(msg_lines) <= 1:
        await message.answer("Statistika hozircha tayyor emas (Meta ma'lumotlarni 24 soat ichida hisoblaydi).")
    else:
        await message.answer("\n".join(msg_lines), disable_web_page_preview=True)


async def _apply_new_ig_token(message: Message, new_token: str) -> None:
    """Validate and save a new Instagram access token."""
    client = InstagramClient(token=new_token)
    try:
        acc = await client.get_account_info()
        username = acc.get("username", "Noma'lum")
        await tokens.save_token(new_token, expires_in_sec=60 * 86400)
        failed = await db.list_failed_posts(limit=3)
        retry_kb = None
        extra_note = "Endi postlarni bemalol rejalashtirishingiz va e'lon qilishingiz mumkin."
        if failed:
            fp = failed[0]
            extra_note = f"To'xtab qolgan Post #{fp.id} ni hoziroq e'lon qilish uchun quyidagi tugmani bosing:"
            retry_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🚀 Post #{fp.id} ni hozir joylash", callback_data=f"ig_retry_{fp.id}")
            ]])
        await message.answer(
            f"✅ <b>Instagram token muvaffaqiyatli yangilandi!</b>\n\n"
            f"👤 Akkaunt: <b>@{username}</b>\n"
            f"🆔 ID: <code>{acc.get('id', '')}</code>\n\n"
            f"{extra_note}",
            reply_markup=retry_kb,
        )
    except Exception as exc:
        err = tokens.redact_token(str(exc))
        await message.answer(
            f"❌ <b>Token tekshiruvdan o'tmadi:</b>\n<i>{escape_html(err)}</i>\n\n"
            "Iltimos, Meta for Developers orqali yangi token oling."
        )


@router.message(Command("igtoken", "settoken"))
async def cmd_set_token(message: Message) -> None:
    """Update Instagram access token from Telegram."""
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "ℹ️ <b>Instagram tokenni yangilash:</b>\n"
            "Yangi tokenni shunchaki chatga yuboring yoki:\n"
            "<code>/igtoken IGAA...</code> ko'rinishida yozing."
        )
        return
    await _apply_new_ig_token(message, parts[1].strip())


@router.message(F.text.regexp(r"^(IGAA|IGQV|EAA)[A-Za-z0-9_-]{40,}$"))
async def on_raw_token_paste(message: Message) -> None:
    """Auto-detect pasted Instagram/Meta access token."""
    await _apply_new_ig_token(message, (message.text or "").strip())


@router.message(Command("iglogin"))
async def cmd_iglogin(message: Message) -> None:
    """Direct Instagram login via instagrapi without Facebook Developer account."""
    from services import ig_direct

    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=2)

    # Delete password message from chat for security
    if len(parts) >= 2:
        try:
            await message.delete()
        except Exception:
            pass

    if len(parts) < 3:
        await message.answer(
            "🔐 <b>Instagram'ga to'g'ridan-to'g'ri ulanish (Facebook'siz):</b>\n\n"
            "Username va parolingizni quyidagicha yuboring:\n"
            "<code>/iglogin username parolingiz</code>\n\n"
            "<i>(Xavfsizlik uchun yuborgan parol xabaringiz chatdan darhol avtomatik o'chiriladi)</i>"
        )
        return

    username = parts[1].strip().lstrip("@")
    password = parts[2].strip()

    status_msg = await message.answer(f"⏳ <b>@{escape_html(username)}</b> profiliga ulanilmoqda, kuting...")

    async def _prompt_code(choice: str) -> None:
        await message.answer(
            f"📱 <b>Instagram tasdiqlash kodini so'radi ({escape_html(choice)})!</b>\n\n"
            "Email yoki SMS orqali kelgan <b>6 xonali kodni</b> shu yerga oddiy xabar qilib yuboring:"
        )

    try:
        acc = await ig_direct.login_direct(
            username=username,
            password=password,
            on_challenge_prompt=_prompt_code,
        )
        failed = await db.list_failed_posts(limit=3)
        retry_kb = None
        extra_note = "Endi postlarni Facebook'siz va tokensiz bemalol e'lon qilishingiz mumkin!"
        if failed:
            fp = failed[0]
            extra_note = f"To'xtab qolgan Post #{fp.id} ni hoziroq e'lon qilish uchun quyidagi tugmani bosing:"
            retry_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🚀 Post #{fp.id} ni hozir joylash", callback_data=f"ig_retry_{fp.id}")
            ]])
        await status_msg.edit_text(
            f"✅ <b>Instagram'ga muvaffaqiyatli ulandingiz!</b>\n\n"
            f"👤 Akkaunt: <b>@{escape_html(acc['username'])}</b>\n"
            f"🆔 ID: <code>{escape_html(acc['id'])}</code>\n"
            f"🔗 Rejim: <b>To'g'ridan-to'g'ri (Facebook'siz)</b>\n\n"
            f"{extra_note}",
            reply_markup=retry_kb,
        )
    except Exception as exc:
        err = escape_html(str(exc)[:200])
        await status_msg.edit_text(
            f"❌ <b>Ulanishda xatolik:</b>\n<i>{err}</i>\n\n"
            "Agar Instagram ilovasida «Bu sizmisiz? (This was me)» degan so'rov chiqqan bo'lsa, "
            "uni tasdiqlab qayta <code>/iglogin username parol</code> yuboring."
        )


@router.message(Command("igsession"))
async def cmd_igsession(message: Message) -> None:
    """Direct Instagram login via browser sessionid cookie."""
    from services import ig_direct

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "🍪 <b>SessionID orqali ulanish:</b>\n"
            "<code>/igsession sessionid_qiymati</code>"
        )
        return

    try:
        await message.delete()
    except Exception:
        pass

    status_msg = await message.answer("⏳ Session tekshirilmoqda...")
    try:
        acc = await ig_direct.login_by_sessionid(parts[1].strip())
        failed = await db.list_failed_posts(limit=3)
        retry_kb = None
        if failed:
            fp = failed[0]
            retry_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🚀 Post #{fp.id} ni hozir joylash", callback_data=f"ig_retry_{fp.id}")
            ]])
        await status_msg.edit_text(
            f"✅ <b>Instagram'ga muvaffaqiyatli ulandingiz!</b>\n\n"
            f"👤 Akkaunt: <b>@{escape_html(acc['username'])}</b>\n"
            f"🆔 ID: <code>{escape_html(acc['id'])}</code>",
            reply_markup=retry_kb,
        )
    except Exception as exc:
        await status_msg.edit_text(f"❌ Xatolik: <i>{escape_html(str(exc)[:150])}</i>")


@router.message(F.text.regexp(r"^\d{6}$"))
async def on_verification_code_input(message: Message) -> None:
    """Capture 6-digit verification code if instagrapi is waiting for challenge resolution."""
    from services import ig_direct

    if ig_direct.is_waiting_for_code():
        code = (message.text or "").strip()
        if ig_direct.submit_verification_code(code):
            await message.answer("✅ Kod qabul qilindi, Instagram bilan tasdiqlanmoqda...")


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


@router.message(Command("rek", "igoptimize"))
async def cmd_rek_optimize(message: Message) -> None:
    """AI-optimize Instagram profile (Name, Bio, Public visibility) for Reels recommendations (Explore/SEO)."""
    from services import ig_direct

    parts = (message.text or "").strip().split(maxsplit=1)
    niche_hint = parts[1].strip() if len(parts) > 1 else ""
    status_msg = await message.answer("🤖 <b>AI Instagram profilingizni tahlil qilib, Rek (SEO) uchun moslashtirmoqda...</b>")

    try:
        res = await ig_direct.optimize_profile_for_rek(niche_hint=niche_hint)
        await status_msg.edit_text(
            f"✅ <b>Instagram profilingiz Rek (Explore & SEO) uchun moslashtirildi!</b>\n\n"
            f"👤 <b>Profil:</b> @{escape_html(res['username'])}\n"
            f"🏷 <b>Avvalgi ism:</b> <s>{escape_html(res['old_name'])}</s>\n"
            f"✨ <b>Yangi SEO ism:</b> <code>{escape_html(res['new_name'])}</code>\n\n"
            f"📝 <b>Yangi SEO Bio:</b>\n<pre>{escape_html(res['new_bio'])}</pre>\n\n"
            f"🔓 <b>Ochiqlik holati:</b> Ommaviy (Public) ✅\n"
            f"🎬 <b>Reels sozlamalari:</b> Profil setkasida ko'rsatish, ulashish (Share/Download) va Smart Tracking yoqildi ✅"
        )
    except Exception as exc:
        await status_msg.edit_text(
            f"❌ <b>Profilni moslashtirishda xatolik:</b>\n<i>{escape_html(str(exc)[:200])}</i>"
        )


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
        info = await intake.fetch(bot, inc, temp_file, require_audio=False)
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
        media_obj = (
            getattr(sent_doc, "document", None)
            or getattr(sent_doc, "video", None)
            or getattr(sent_doc, "animation", None)
        )
        if not media_obj:
            raise ValueError(f"Telegram response contained no video or document: {sent_doc}")

        tg_file_id = media_obj.file_id
        tg_unique_id = media_obj.file_unique_id
        file_size = getattr(media_obj, "file_size", None) or out_file.stat().st_size
        storage_msg_id = sent_doc.message_id
    except Exception as exc:
        log.exception("could not store video to chat %s: %s", storage_chat, exc)
        try:
            await status_msg.edit_text("Videoni saqlashda xatolik yuz berdi. Qayta urinib ko'ring.")
        except Exception:
            pass
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
        file_size=file_size,
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
    auto_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Avto (AI o'zi yozsin)", callback_data="ig_desc_auto"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="ig_cancel_flow"),
    ]])
    await bot.send_message(chat_id, texts.REEL_ASK_DESC, reply_markup=auto_kb)


# ----------------------------------------------------------------------
# Description Intake & AI Caption Generation
# ----------------------------------------------------------------------

@router.callback_query(IgStates.waiting_description, F.data == "ig_desc_auto")
async def on_desc_auto(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Handle skipping description input, letting AI generate solely from video frames."""
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    data = await state.get_data()
    post_id = data.get("post_id")
    if not post_id:
        await state.clear()
        await cb.message.answer(texts.SESSION_EXPIRED)
        return

    import base64
    frames = [base64.b64decode(b) for b in data.get("frames_b64", [])]
    audio_tags = {"title": data.get("title", ""), "artist": data.get("artist", "")}

    status = await cb.message.answer(texts.REEL_AI_WORKING)
    ai_res = await llm.generate_caption(
        description="Kompaniya, restoran va bizneslar uchun zamonaviy veb-sayt va Telegram bot yaratish xizmati (zakaz olish uchun)",
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
    await state.update_data(last_desc="Kompaniya, restoran va bizneslar uchun zamonaviy veb-sayt va Telegram bot yaratish xizmati")

    preview_text = texts.REEL_PREVIEW_CARD.format(
        caption=escape_html(caption),
        hashtags=escape_html(" ".join("#" + t for t in hashtags)),
    )
    if ai_res.get("is_fallback"):
        preview_text += f"\n\n⚠️ <i>{texts.REEL_AI_FALLBACK}</i>"

    await cb.message.answer(preview_text, reply_markup=kb.reel_preview_kb(post_id))

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
    if desc_text.strip().lower() in ("avto", "auto", "o'tkazish", "otkazish", "-", "."):
        desc_text = "Kompaniya, restoran va bizneslar uchun zamonaviy veb-sayt va Telegram bot yaratish xizmati (zakaz olish uchun)"
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
