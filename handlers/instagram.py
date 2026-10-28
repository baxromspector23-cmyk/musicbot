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


async def _build_stats_report() -> tuple[str, InlineKeyboardMarkup]:
    """Build real-time Instagram profile, Reels views/likes/comments, and Autopilot status report."""
    from services import autopilot, ig_direct

    tz = ZoneInfo(config.TZ)
    scheduled = await db.list_scheduled_posts(limit=20)
    ap_on = await autopilot.is_autopilot_enabled()
    ap_badge = "🟢 Yoqilgan (09:00, 11:00, 12:00, 18:30, 20:30)" if ap_on else "🔴 O'chirilgan"

    stats_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Yangilash", callback_data="ig_stats_refresh"),
            InlineKeyboardButton(text="🤖 Avtopilot (5x/kun)", callback_data="ig_autopilot_menu"),
        ],
    ])

    st = None
    is_cached = False
    if await ig_direct.has_direct_session():
        try:
            st = await asyncio.wait_for(ig_direct.get_live_profile_stats(limit=8), timeout=15.0)
        except Exception as exc:
            log.warning("Live direct stats error: %s, falling back to cached stats", exc)
            cached_data = await db.get_kv("ig_cached_profile_stats")
            if cached_data and isinstance(cached_data, dict):
                st = cached_data
                is_cached = True

    if st:
        header_tag = "📊 <b>Instagram Jonli Statistika (Real-Time)</b>\n" if not is_cached else "📊 <b>Instagram Statistikasi</b> <i>(Oxirgi ma'lumotlar)</i>\n"
        lines = [
            header_tag,
            f"👤 <b>Profil:</b> <a href='https://www.instagram.com/{escape_html(st.get('username', 'coder_bahrom'))}/'>@{escape_html(st.get('username', 'coder_bahrom'))}</a> ({escape_html(st.get('full_name', ''))})",
            f"👥 <b>Obunachilar:</b> <code>{st.get('followers', 0):,}</code> | <b>Obunalar:</b> <code>{st.get('following', 0):,}</code> | <b>Postlar:</b> <code>{st.get('media_count', 0)}</code>",
            "",
            "📈 <b>Umumiy Reels Ko'rsatkichlari:</b>",
            f"👁 Jami ko'rishlar: <b>{st.get('total_views', 0):,}</b> (o'rtacha: <code>{st.get('avg_views', 0):,}</code> / video)",
            f"❤️ Jami layklar: <b>{st.get('total_likes', 0):,}</b> | 💬 Izohlar: <b>{st.get('total_comments', 0):,}</b>",
            "",
            "🎬 <b>Oxirgi joylangan Reels videolar:</b>",
        ]
        items = st.get("items", [])
        if items:
            for idx, item in enumerate(items, 1):
                dt_str = ""
                taken_at = item.get("taken_at")
                if taken_at:
                    try:
                        if isinstance(taken_at, str):
                            parsed_dt = datetime.fromisoformat(taken_at)
                            dt_str = parsed_dt.astimezone(tz).strftime("%d.%m %H:%M") + " | "
                        elif hasattr(taken_at, "astimezone"):
                            dt_str = taken_at.astimezone(tz).strftime("%d.%m %H:%M") + " | "
                    except Exception:
                        pass
                title = escape_html(item.get("title", "Reel video"))
                link = escape_html(item.get("permalink", ""))
                lines.append(
                    f"{idx}. {dt_str}<a href='{link}'>{title}</a>\n"
                    f"   👁 <b>{item.get('views', 0):,}</b> ko'rish | ❤️ <b>{item.get('likes', 0):,}</b> | 💬 <b>{item.get('comments', 0):,}</b>"
                )
        else:
            lines.append("<i>Hozircha videolar yo'q.</i>")

        lines.extend([
            "",
            f"🤖 <b>Avtopilot (Kuniga 5 ta Rek):</b> {ap_badge}",
            f"📅 <b>Navbatdagi rejalashtirilgan videolar:</b> <code>{len(scheduled)} ta</code> (/queue)",
        ])
        return "\n".join(lines), stats_kb

    # Fallback to DB published posts
    posts = await db.list_published_posts(limit=8)
    if not posts:
        extra = f"\n\n🤖 <b>Avtopilot:</b> {ap_badge}\n📅 <b>Navbatda:</b> {len(scheduled)} ta post (/queue)"
        return texts.STATS_EMPTY + extra, stats_kb

    msg_lines = [
        "📊 <b>Instagram Joylangan Videolar Ro'yxati:</b>\n",
        f"🤖 <b>Avtopilot (Kuniga 5 ta):</b> {ap_badge}",
        f"📅 <b>Navbatdagi videolar:</b> <code>{len(scheduled)} ta</code> (/queue)\n",
        "🎬 <b>E'lon qilingan Reels videolar:</b>",
    ]
    for idx, p in enumerate(posts, 1):
        loc_time = p.published_at.astimezone(tz).strftime("%d.%m %H:%M") if p.published_at else "—"
        cap = escape_html((p.caption or "")[:40].replace("\n", " "))
        link = escape_html(p.permalink or "—")
        msg_lines.append(f"{idx}. {loc_time} | <a href='{link}'>{cap}</a>")

    return "\n".join(msg_lines), stats_kb


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Show live profile & Reels insights for @coder_bahrom."""
    wait_msg = await message.answer("⏳ <b>Instagram'dan jonli statistika olinmoqda...</b>")
    report_text, markup = await _build_stats_report()
    await wait_msg.edit_text(report_text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "ig_stats_refresh")
async def on_stats_refresh(cb: CallbackQuery) -> None:
    """Refresh live Instagram stats inline."""
    await cb.answer("🔄 Jonli ma'lumotlar yangilanmoqda...")
    report_text, markup = await _build_stats_report()
    try:
        await cb.message.edit_text(report_text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        pass


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
    """Display Instagram account info, token/session status, and insights availability."""
    from services import autopilot, ig_direct

    queue = await db.list_scheduled_posts(limit=100)
    ap_on = await autopilot.is_autopilot_enabled()

    if await ig_direct.has_direct_session():
        try:
            st = await ig_direct.get_live_profile_stats(limit=3)
            ap_str = "🟢 Yoqilgan (09:00, 11:30, 18:30, 20:30)" if ap_on else "🔴 O'chirilgan"
            await message.answer(
                f"📸 <b>Instagram Holati (To'g'ridan-to'g'ri rejim):</b>\n\n"
                f"👤 <b>Akkaunt:</b> @{escape_html(st['username'])} ({escape_html(st['full_name'])})\n"
                f"👥 <b>Obunachilar:</b> <code>{st['followers']:,}</code> | <b>Postlar:</b> <code>{st['media_count']}</code>\n"
                f"🔑 <b>Ulanish:</b> Faol (Facebook'siz Direct Session ✅)\n"
                f"🤖 <b>Avtopilot (4x/kun):</b> {ap_str}\n"
                f"📋 <b>Navbatdagi postlar:</b> <code>{len(queue)} ta</code> (/queue)\n"
                f"📊 <b>Jonli statistika:</b> /stats"
            )
            return
        except Exception as exc:
            log.warning("igstatus direct check fallback: %s", exc)

    token = await tokens.get_active_token()
    tok_info = await tokens.get_token_status()

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


async def _build_autopilot_card() -> tuple[str, InlineKeyboardMarkup]:
    from services import autopilot

    enabled = await autopilot.is_autopilot_enabled()
    queue = await db.list_scheduled_posts(limit=20)
    tz = ZoneInfo(config.TZ)

    status_str = "🟢 <b>YOQILGAN (Faol)</b>" if enabled else "🔴 <b>O'CHIRILGAN</b>"
    toggle_btn_text = "🔴 Avtopilotni o'chirish" if enabled else "🟢 Avtopilotni yoqish"

    lines = [
        "🤖 <b>Avtomatik Video Topish va Joylash (Avtopilot 4x/kun)</b>\n",
        f"⚙️ <b>Holati:</b> {status_str}",
        "📅 <b>Kunlik «Rek» chiqish vaqtlari (Asia/Tashkent):</b>",
        "  • 🌅 <b>09:00</b> — Ertalabki Rek #1 (Restoran & Onlayn Menyu)",
        "  • ☀️ <b>11:30</b> — Ertalabki Rek #2 (Kompaniya & Veb-Sayt)",
        "  • 🌆 <b>18:30</b> — Kechki Rek #1 (Internet-Do'kon & Katalog)",
        "  • 🌙 <b>20:30</b> — Kechki Rek #2 (Telegram Bot & Avtomatlashtirish)\n",
        f"📋 <b>Hozir navbatda tayyor turgan videolar:</b> <code>{len(queue)} ta</code>",
    ]
    for p in queue[:4]:
        loc_t = p.scheduled_at.astimezone(tz).strftime("%d.%m %H:%M") if p.scheduled_at else "—"
        cap_short = escape_html((p.caption or "").splitlines()[0][:38])
        lines.append(f"  ▫️ <b>#{p.id}</b> | <code>{loc_t}</code> — <i>{cap_short}</i>")

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Hozir 4 ta Rek vaqtini videoga to'ldirish", callback_data="ap_fill_4")],
        [
            InlineKeyboardButton(text=toggle_btn_text, callback_data="ap_toggle"),
            InlineKeyboardButton(text="📊 Jonli Statistika", callback_data="ig_stats_refresh"),
        ],
    ])
    return "\n".join(lines), markup


@router.message(Command("autopilot", "auto"))
async def cmd_autopilot(message: Message) -> None:
    """Manage 4x/day automatic video finder and Rek scheduler."""
    text, markup = await _build_autopilot_card()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ig_autopilot_menu")
async def on_autopilot_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    text, markup = await _build_autopilot_card()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ap_toggle")
async def on_autopilot_toggle(cb: CallbackQuery) -> None:
    from services import autopilot

    curr = await autopilot.is_autopilot_enabled()
    await autopilot.set_autopilot_enabled(not curr)
    await cb.answer("Avtopilot yoqildi! ✅" if not curr else "Avtopilot o'chirildi! ⏸")
    text, markup = await _build_autopilot_card()
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


@router.callback_query(F.data == "ap_fill_4")
async def on_autopilot_fill_4(cb: CallbackQuery, bot: Bot) -> None:
    from services import autopilot

    await cb.answer("🔍 Videolar qidirilmoqda va tayyorlanmoqda...")
    status_msg = await cb.message.answer(
        "🤖 <b>Avtopilot ishga tushdi:</b>\n"
        "Ertalabki (09:00, 11:30) va kechki (18:30, 20:30) Rek vaqtlari uchun "
        "yangi videolar topilib, 9:16 formatda tayyorlanmoqda (1-2 daqiqa)..."
    )
    created = await autopilot.ensure_daily_autopilot_posts(bot, notify_owner=False)
    try:
        await status_msg.delete()
    except Exception:
        pass

    text, markup = await _build_autopilot_card()
    note = (
        f"✅ <b>{len(created)} ta yangi video topildi va Rek vaqtlariga rejalashtirildi!</b>\n\n"
        if created
        else "✅ <b>Barcha 4 ta Rek vaqti allaqachon videolar bilan to'ldirilgan!</b>\n\n"
    )
    await cb.message.answer(note + text, reply_markup=markup)


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
