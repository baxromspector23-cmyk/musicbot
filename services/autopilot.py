"""Automated B2B Reel discovery, normalization, and 4x/day Rek scheduling (2 morning + 2 evening)."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram.types import FSInputFile

import config
from services import db, ig_direct
from utils import escape_html

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger(__name__)

# 5 daily optimal Rek slots in Asia/Tashkent:
# - Morning: 09:00, 11:00
# - Midday: 12:00
# - Evening: 18:30, 20:30
DAILY_SLOTS_SPEC: list[dict[str, Any]] = [
    {
        "time": "09:00",
        "label": "🌅 Ertalabki Rek #1 (09:00)",
        "niche": "Restoran va Kafe Onlayn Menyu",
        "search_tags": ["webdesign", "uidesign", "uxdesign", "website"],
        "caption": (
            "🍽 Restoran yoki kafengiz uchun mijozlarni jalb qiluvchi Zamonaviy Sayt va Onlayn Menyu! ✨\n\n"
            "Mijozlaringiz telefon orqali menyuni ko'rib, birgina tugma bilan buyurtma berishini xohlaysizmi?\n"
            "✅ Interaktiv 3D / foto menyu va QR-kod tizimi\n"
            "✅ Yetkazib berish (Dostavka) va bron qilish funksiyasi\n"
            "✅ Telegram bot bilan to'liq integratsiya\n\n"
            "📈 Restoraningiz savdosini 2 barobarga oshiring!\n"
            "📩 Zakaz berish uchun Direct (DM) ga «MENYU» yoki «ZAKAZ» deb yozing!"
        ),
        "hashtags": ["restoranuz", "saytyaratish", "biznesuz", "tadbirkor", "telegrambot"],
    },
    {
        "time": "11:00",
        "label": "☀️ Ertalabki Rek #2 (11:00)",
        "niche": "Kompaniya va Korporativ Veb-Sayt",
        "search_tags": ["webdeveloper", "frontend", "webdesign", "coding"],
        "caption": (
            "💼 Kompaniyangiz nufuzini va savdosini oshiruvchi Zamonaviy Veb-Sayt kerakmi? 🚀\n\n"
            "Har bir loyihani 0 dan boshlab, zamonaviy dizayn va tezkor kod asosida tayyorlab beramiz:\n"
            "✅ Kompaniya va xizmat ko'rsatish bizneslari uchun Korporativ Sayt\n"
            "✅ Yuqori konversiyali Landing Page\n"
            "✅ Google va Instagram qidiruviga (SEO) moslashtirilgan tizim\n\n"
            "🤝 Biznesingizni onlaynda yangi bosqichga olib chiqing!\n"
            "📩 Loyiha buyurtma qilish uchun Direct (DM) ga «ZAKAZ» deb yozing!"
        ),
        "hashtags": ["saytyaratish", "biznesuz", "kompaniya", "vebsayt", "tadbirkor"],
    },
    {
        "time": "12:00",
        "label": "🔥 Kunduzgi Rek (12:00)",
        "niche": "Zamonaviy UI/UX va Interaktiv Veb-Saytlar",
        "search_tags": ["webdevelopment", "uiux", "interactivedesign", "javascript"],
        "caption": (
            "✨ Biznesingiz uchun eng so'nggi trenddagi Zamonaviy va Interaktiv Veb-Sayt! 🚀\n\n"
            "Oddiy saytlar mijozni ushlab qololmaydi. Biz yaratadigan saytlar esa mijozni bir ko'rishda hayratda qoldiradi va buyurtma berishga undaydi:\n"
            "✅ 3D elementlar va silliq interaktiv animatsiyalar\n"
            "✅ Telefon va noutbuklarda maksimal yuklanish tezligi (< 0.5s)\n"
            "✅ Onlayn to'lov va to'liq avtomatlashtirilgan CRM tizimi\n\n"
            "💼 Saytingiz siz uchun 24/7 yangi mijozlar olib kelsin!\n"
            "📩 Loyihangizni boshlash uchun Direct (DM) ga «SAYT» deb yozing!"
        ),
        "hashtags": ["vebsayt", "saytyaratish", "biznesuz", "tadbirkor", "dizayn"],
    },
    {
        "time": "18:30",
        "label": "🌆 Kechki Rek #1 (18:30)",
        "niche": "Do'konlar uchun Internet-Do'kon & Katalog",
        "search_tags": ["appdesign", "uidesign", "webdesign", "uxdesign"],
        "caption": (
            "🛍 Do'koningiz savdosini 24/7 to'xtovsiz ishlaydigan Onlayn Do'kon bilan oshiring! ✨\n\n"
            "Mijozlar mahsulotlaringizni qulay katalogda ko'rib, darhol xarid qilishlari uchun zamonaviy yechim:\n"
            "✅ Barcha telefon va kompyuterlarga moslashuvchan dizayn\n"
            "✅ Click / Payme to'lov tizimlari va savatcha funksiyasi\n"
            "✅ Buyurtmalarni Telegram orqali tezkor qabul qilish\n\n"
            "📈 Savdoingizni avtomatlashtirish vaqti keldi!\n"
            "📩 Zakaz berish uchun Direct (DM) ga «DOKON» yoki «ZAKAZ» deb yozing!"
        ),
        "hashtags": ["internetdokon", "saytyaratish", "biznesuz", "tadbirkor", "savdo"],
    },
    {
        "time": "20:30",
        "label": "🌙 Kechki Rek #2 (20:30)",
        "niche": "Telegram Bot va Biznes Avtomatlashtirish",
        "search_tags": ["coding", "programming", "webdeveloper", "softwaredeveloper"],
        "caption": (
            "🤖 Mijozlardan 24/7 zakaz qabul qiluvchi Zamonaviy Sayt va Telegram Bot! ⚡\n\n"
            "Kod qatorlaridan — tayyor daromad keltiruvchi biznes tizimgacha:\n"
            "✅ Restoran, do'kon va kompaniyalar uchun Maxsus Telegram Botlar\n"
            "✅ Telegram Mini-App (WebApp) onlayn katalog va buyurtma tizimi\n"
            "✅ Mijozlar bazasi va avtomat hisobotlar\n\n"
            "💼 Biznesingiz uchun individual loyiha kerakmi?\n"
            "📩 Hoziroq Direct (DM) ga «ZAKAZ» deb yozing — bepul maslahat beramiz!"
        ),
        "hashtags": ["telegrambot", "saytyaratish", "biznesuz", "tadbirkor", "dasturchi"],
    },
]

_LAST_AUTOPILOT_CHECK_TS: float = 0.0
_AUTOPILOT_LOCK = asyncio.Lock()


async def is_autopilot_enabled() -> bool:
    """Return True if 4x/day autopilot is enabled (defaults to True)."""
    val = await db.get_kv("autopilot_enabled", None)
    if val is None:
        await db.set_kv("autopilot_enabled", {"enabled": True, "updated_at": time.time()})
        return True
    if isinstance(val, dict):
        return bool(val.get("enabled", True))
    return bool(val)


async def set_autopilot_enabled(enabled: bool) -> None:
    """Enable or disable 4x/day autopilot."""
    await db.set_kv("autopilot_enabled", {"enabled": bool(enabled), "updated_at": time.time()})


async def _get_used_codes() -> set[str]:
    data = await db.get_kv("autopilot_used_codes", None)
    if isinstance(data, dict) and isinstance(data.get("codes"), list):
        return set(str(x) for x in data["codes"])
    return set()


async def _mark_code_used(code: str) -> None:
    used = await _get_used_codes()
    used.add(code)
    # Keep last 300 codes
    trimmed = list(used)[-300:]
    await db.set_kv("autopilot_used_codes", {"codes": trimmed, "updated_at": time.time()})


async def find_and_download_b2b_video(slot_index: int = 0) -> dict[str, Any]:
    """Search Instagram for a high-engagement coding/web-showcase Reel, download and normalize to 9:16."""
    settings = await ig_direct.load_direct_settings()
    if not settings:
        raise RuntimeError("Instagram akkaunt ulanmagan (/iglogin orqali ulang).")

    used_codes = await _get_used_codes()
    spec = DAILY_SLOTS_SPEC[slot_index % len(DAILY_SLOTS_SPEC)]
    search_tags = spec["search_tags"]

    temp_dir = config.TEMP_ROOT / "autopilot_reels"
    temp_dir.mkdir(parents=True, exist_ok=True)

    def _search_and_download() -> dict[str, Any]:
        cl = ig_direct._create_client(settings)
        my_uid = str(cl.user_id or "")

        candidates: list[Any] = []
        for tag in search_tags:
            try:
                medias = cl.hashtag_medias_top_v1(tag, amount=15)
                for m in medias:
                    code = str(getattr(m, "code", "") or "")
                    dur = float(getattr(m, "video_duration", 0) or 0)
                    mtype = int(getattr(m, "media_type", 0) or 0)
                    owner_pk = str(getattr(getattr(m, "user", None), "pk", "") or "")
                    if mtype == 2 and 6.0 <= dur <= 38.0 and code and code not in used_codes and owner_pk != my_uid:
                        candidates.append(m)
                if len(candidates) >= 5:
                    break
            except Exception as exc:
                log.debug("hashtag top search %s warning: %s", tag, exc)
                try:
                    medias = cl.hashtag_medias_recent_v1(tag, amount=15)
                    for m in medias:
                        code = str(getattr(m, "code", "") or "")
                        dur = float(getattr(m, "video_duration", 0) or 0)
                        mtype = int(getattr(m, "media_type", 0) or 0)
                        owner_pk = str(getattr(getattr(m, "user", None), "pk", "") or "")
                        if mtype == 2 and 6.0 <= dur <= 38.0 and code and code not in used_codes and owner_pk != my_uid:
                            candidates.append(m)
                except Exception:
                    pass

        if not candidates:
            raise RuntimeError("Mos video topilmadi, keyingi urinishda qayta qidiriladi.")

        # Sort by engagement (views + likes * 10)
        candidates.sort(
            key=lambda x: int(getattr(x, "play_count", 0) or 0) + int(getattr(x, "like_count", 0) or 0) * 10,
            reverse=True,
        )
        chosen = candidates[0]
        code = str(chosen.code)
        dl_path = Path(cl.video_download(chosen.pk, folder=temp_dir))
        norm_path = temp_dir / f"norm_{code}.mp4"

        # Normalize to 1080x1920 9:16 H.264 + AAC
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "quiet",
                "-i", str(dl_path),
                "-t", "28",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                str(norm_path),
            ],
            check=True,
            timeout=180,
        )
        dl_path.unlink(missing_ok=True)
        return {
            "path": norm_path,
            "code": code,
            "duration": min(28.0, float(getattr(chosen, "video_duration", 15.0) or 15.0)),
        }

    res = await asyncio.to_thread(_search_and_download)
    await _mark_code_used(res["code"])
    return res


def get_next_4_slot_datetimes(now_utc: datetime | None = None) -> list[tuple[int, datetime, dict[str, Any]]]:
    """Return the next 4 upcoming Rek slot UTC datetimes (2 morning: 09:00, 11:30; 2 evening: 18:30, 20:30)."""
    now = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(config.TZ)
    local_now = now.astimezone(tz)

    upcoming: list[tuple[int, datetime, dict[str, Any]]] = []
    for day_offset in (0, 1, 2):
        base_date = (local_now + timedelta(days=day_offset)).date()
        for idx, spec in enumerate(DAILY_SLOTS_SPEC):
            h_str, m_str = spec["time"].split(":")
            slot_local = datetime(
                base_date.year, base_date.month, base_date.day,
                int(h_str), int(m_str), 0, tzinfo=tz,
            )
            slot_utc = slot_local.astimezone(timezone.utc)
            if slot_utc > now + timedelta(minutes=8):
                upcoming.append((idx, slot_utc, spec))
                if len(upcoming) >= 4:
                    return upcoming
    return upcoming[:4]


async def ensure_daily_autopilot_posts(bot: Bot, notify_owner: bool = True) -> list[db.Post]:
    """Ensure all 4 upcoming Rek slots (2 morning + 2 evening) have a scheduled Reel ready."""
    if _AUTOPILOT_LOCK.locked():
        return []

    async with _AUTOPILOT_LOCK:
        if not await ig_direct.has_direct_session():
            return []

        now_utc = datetime.now(timezone.utc)
        tz = ZoneInfo(config.TZ)
        next_4 = get_next_4_slot_datetimes(now_utc)
        existing_posts = await db.list_scheduled_posts(limit=100)
        existing_times = [p.scheduled_at for p in existing_posts if p.scheduled_at is not None]

        created_posts: list[db.Post] = []
        storage_chat = config.STORAGE_CHAT_ID or config.OWNER_ID

        for slot_idx, slot_utc, spec in next_4:
            # Check if there is already a post scheduled within 75 minutes of this slot
            already_covered = any(
                abs((ex_dt - slot_utc).total_seconds()) < 75 * 60
                for ex_dt in existing_times
            )
            if already_covered:
                continue

            try:
                vid_info = await find_and_download_b2b_video(slot_idx)
                norm_path: Path = vid_info["path"]
                try:
                    sent_doc = await bot.send_document(
                        storage_chat,
                        document=FSInputFile(norm_path, filename=f"autopilot_{vid_info['code']}.mp4"),
                        caption=f"🤖 Avtopilot Reel ({spec['label']} — {spec['niche']})",
                    )
                    media_obj = (
                        getattr(sent_doc, "document", None)
                        or getattr(sent_doc, "video", None)
                        or getattr(sent_doc, "animation", None)
                    )
                    if not media_obj:
                        continue

                    full_caption = f"{spec['caption']}\n\n{' '.join('#' + t for t in spec['hashtags'])}"
                    post = await db.create_post(
                        owner_id=config.OWNER_ID,
                        storage_chat_id=storage_chat,
                        storage_message_id=sent_doc.message_id,
                        tg_file_id=media_obj.file_id,
                        file_unique_id=media_obj.file_unique_id,
                        file_size=getattr(media_obj, "file_size", None) or norm_path.stat().st_size,
                        duration=vid_info["duration"],
                        width=1080,
                        height=1920,
                        caption=full_caption,
                        hashtags=spec["hashtags"],
                        scheduled_at=slot_utc,
                        status="scheduled",
                    )
                    created_posts.append(post)
                    existing_times.append(slot_utc)
                finally:
                    norm_path.unlink(missing_ok=True)
            except Exception as exc:
                log.warning("Autopilot slot %s preparation failed: %s", spec["time"], exc)

        if created_posts and notify_owner:
            lines = ["🤖 <b>Avtopilot: Kunlik 4 ta «Rek» vaqtiga yangi videolar topildi va rejalashtirildi!</b>\n"]
            for p in created_posts:
                loc_str = p.scheduled_at.astimezone(tz).strftime("%d.%m %H:%M") if p.scheduled_at else "—"
                first_line = escape_html((p.caption or "").splitlines()[0][:45])
                lines.append(f"• <b>#{p.id}</b> | ⏰ <code>{loc_str}</code> — <i>{first_line}</i>")
            lines.append("\n📋 Navbatni ko'rish va boshqarish: /queue | ⚙️ Avtopilot: /autopilot")
            try:
                await bot.send_message(config.OWNER_ID, "\n".join(lines))
            except Exception:
                pass

        return created_posts


async def maybe_run_autopilot_tick(bot: Bot) -> None:
    """Periodic check called from scheduler loop every 30 minutes to keep 4 daily slots filled."""
    global _LAST_AUTOPILOT_CHECK_TS
    now_ts = time.time()
    if now_ts - _LAST_AUTOPILOT_CHECK_TS < 1800:  # Check every 30 minutes
        return
    _LAST_AUTOPILOT_CHECK_TS = now_ts

    if not await is_autopilot_enabled():
        return
    try:
        await ensure_daily_autopilot_posts(bot, notify_owner=True)
    except Exception as exc:
        log.warning("maybe_run_autopilot_tick error: %s", exc)
