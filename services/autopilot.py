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
INDIAN_AND_UNWANTED_KEYWORDS: set[str] = {
    # Countries, regions, cities, states, institutes
    "india", "indian", "desi", "desicoder", "delhi", "mumbai", "bangalore", "bengaluru",
    "hyderabad", "pune", "chennai", "kolkata", "noida", "gurgaon", "iit", "nit", "btech",
    # Hindi romanized words
    "hindi", "hinglish", "bhai", "yaar", "karo", "hai", "kare", "kaise", "seekho", "dost",
    "karna", "samjho", "dekho", "dekhiye", "chahiye", "rakho", "wala", "wali", "mera", "meri",
    "apna", "apni", "paisa", "rupee", "rupees", "crore", "lakh", "roopya", "joblagi",
    "shuru", "kijiye", "padho", "bolo", "namaste", "namaskar", "bhaiya", "didi", "sir", "bhau",
    # Typical Indian corporate tags/memes
    "tcs", "infosys", "wipro", "cognizant", "accentureindia", "hcl",
    # Indian names common in handles/usernames/full names
    "singh", "sharma", "patel", "kumar", "gupta", "verma", "yadav", "choudhary", "reddy",
    "nair", "joshi", "mehta", "shah", "das", "roy", "mishra", "pandey", "tiwari", "babu",
    "chaurasia", "deshmukh", "shukla", "agarwal", "bhat", "chaudhary", "mukherjee",
    # Talking-head / influencer / course spam indicators (NO human talking heads allowed)
    "roadmap", "interview", "salary", "placement", "college", "student", "resume",
    "coach", "mentor", "internship", "hiring", "course", "degree", "lecture", "tutorial",
    "explaining", "explain", "listen", "tips",
}


def is_unwanted_content(media: Any) -> bool:
    """Return True if media has Hindi/Devanagari text, Indian keywords, or talking-head markers."""
    import re
    caption = str(getattr(media, "caption_text", "") or "").lower()

    # 1. Check Devanagari script (Hindi characters range: \u0900-\u097F)
    for ch in caption:
        if "\u0900" <= ch <= "\u097f":
            return True

    # 2. Check username and full_name
    user = getattr(media, "user", None)
    username = str(getattr(user, "username", "") or "").lower()
    full_name = str(getattr(user, "full_name", "") or "").lower()

    for ch in f"{username} {full_name}":
        if "\u0900" <= ch <= "\u097f":
            return True

    # 3. Check tokens for Indian keywords
    tokens = set(re.findall(r"[a-z]+", f"{caption} {username} {full_name}"))
    if tokens.intersection(INDIAN_AND_UNWANTED_KEYWORDS):
        return True

    return False


def has_human_face_or_person(video_path: Path) -> tuple[bool, str]:
    """Inspect video frames with OpenCV Haar Cascades and YCrCb skin-tone detector.
    Returns (True, reason) if any human face or person is detected, (False, '') if 100% faceless screen/setup.
    """
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return False, ""

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            cap.release()
            return False, ""

        # Load OpenCV Haar cascade classifiers
        cascades = []
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
            p = getattr(cv2, "data", None) and getattr(cv2.data, "haarcascades", "") + name
            if p and Path(p).exists():
                c = cv2.CascadeClassifier(p)
                if not c.empty():
                    cascades.append((name, c))

        # Sample 8 evenly spaced points across the video
        pts = [0.10, 0.22, 0.35, 0.50, 0.65, 0.78, 0.88, 0.96]
        for pt in pts:
            f_idx = int(pt * total_frames)
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            h, w = frame.shape[:2]
            if h < 100 or w < 100:
                continue

            scale = 480.0 / max(h, w)
            small = cv2.resize(frame, (int(w * scale), int(h * scale)))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            # 1. Cascade face detection
            for c_name, casc in cascades:
                faces = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(28, 28))
                if len(faces) > 0:
                    cap.release()
                    return True, f"human face detected ({c_name} at {int(pt*100)}%)"

            # 2. Skin-tone detection in center region (talking heads occupy center)
            sh, sw = small.shape[:2]
            center = small[int(sh * 0.12):int(sh * 0.88), int(sw * 0.15):int(sw * 0.85)]
            ycrcb = cv2.cvtColor(center, cv2.COLOR_BGR2YCrCb)
            # Standard human skin range in YCrCb: 133 <= Cr <= 173 and 77 <= Cb <= 127
            skin_mask = cv2.inRange(ycrcb, np.array([0, 133, 77]), np.array([255, 173, 127]))
            skin_ratio = float(np.count_nonzero(skin_mask)) / float(skin_mask.shape[0] * skin_mask.shape[1])
            if skin_ratio > 0.11:  # >11% skin in center frame = person talking on camera
                cap.release()
                return True, f"human skin/body detected ({skin_ratio*100:.1f}% at {int(pt*100)}%)"

        cap.release()
        return False, ""
    except Exception as exc:
        log.warning("face detection exception (pass through): %s", exc)
        return False, ""


# 5 daily optimal Rek slots in Asia/Tashkent (Special 2-Day GitHub & Hack Scripts Edition):
DAILY_SLOTS_SPEC: list[dict[str, Any]] = [
    {
        "time": "09:00",
        "label": "🌅 Ertalabki Rek #1 (09:00)",
        "niche": "GitHub Open-Source & Dasturchi Skriptlari",
        "search_tags": [
            "terminalaesthetic", "unixporn", "neovim", "archlinux", "dotfiles", "commandline", "hyprland",
        ],
        "captions": [
            (
                "⚡️ Har bir dasturchi bilishi shart bo'lgan Top GitHub skriptlari! 💻🔥\n\n"
                "Ochiq kodli (Open-source) eng qulay va kuchli vositalar orqali ishingizni 10x tezlashtiring:\n"
                "✅ Bepul va ochiq kodli GitHub loyihalari\n"
                "✅ Kod yozish va vazifalarni avtomatlashtirish\n"
                "✅ Terminalda birgina buyruq bilan ishga tushirish\n\n"
                "🚀 Profilimizda har kuni eng zo'r IT xaklar va dasturlash sirlari!\n"
                "Obuna bo'ling: @coder_bahrom"
            ),
            (
                "🔥 GitHub dagi aqlbovar qilmas foydali ochiq kodli loyihalar! 🛠✨\n\n"
                "Bular haqida ko'pchilik bilmaydi, lekin har bir dasturchining hayotini yengillashtiradi:\n"
                "✅ Qidiruv va kod tahlili uchun tayyor skriptlar\n"
                "✅ Ish unumdorligini oshiruvchi CLI vositalar\n"
                "✅ Ochiq kod — istalgan loyihaga moslashtiring\n\n"
                "💎 Foydali bo'lsa saqlab qo'ying va do'stlarga ulashing!\n"
                "Kanalimiz: @coder_bahrom"
            ),
            (
                "🚀 GitHub dagi eng sara skriptlar va dasturchi xaklari! ⚡️\n\n"
                "Noldan yozib o'tirmang — hammasi GitHub da tayyor va ochiq:\n"
                "✅ Avtomatlashtirilgan repozitoriylar\n"
                "✅ CLI va terminal skriptlari\n"
                "✅ Ishlab chiquvchilar uchun eng yangi trendlar\n\n"
                "💻 Ko'proq IT sirlari uchun sahifamizni kuzatib boring: @coder_bahrom"
            ),
        ],
        "caption": (
            "⚡️ Har bir dasturchi bilishi shart bo'lgan Top GitHub skriptlari! 💻🔥\n\n"
            "Ochiq kodli (Open-source) eng qulay va kuchli vositalar orqali ishingizni 10x tezlashtiring:\n"
            "✅ Bepul va ochiq kodli GitHub loyihalari\n"
            "✅ Kod yozish va vazifalarni avtomatlashtirish\n"
            "✅ Terminalda birgina buyruq bilan ishga tushirish\n\n"
            "🚀 Profilimizda har kuni eng zo'r IT xaklar va dasturlash sirlari!\n"
            "Obuna bo'ling: @coder_bahrom"
        ),
        "hashtags": ["terminalaesthetic", "dasturlash", "ituz", "coder", "dasturchi", "opensource", "neovim"],
    },
    {
        "time": "11:00",
        "label": "☀️ Ertalabki Rek #2 (11:00)",
        "niche": "Terminal & CLI Xaklari / Tezkor Skriptlar",
        "search_tags": [
            "terminalaesthetic", "clitools", "linuxcommands", "neovim", "commandline", "bash", "hyprland", "codingsetup",
        ],
        "captions": [
            (
                "⚡️ Hech kim bilmaydigan eng kuchli Terminal xaklari va CLI skriptlar! 💻🔥\n\n"
                "Sichqonchani unuting — dasturchilar va IT mutaxassislari hamma vazifani terminalda 5 baravar tezroq bajaradi!\n\n"
                "✅ Ishni 10x tezlashtiruvchi CLI buyruqlar\n"
                "✅ Tezkor navigatsiya va fayllarni boshqarish\n"
                "✅ Zsh, Bash va Neovim ajoyib konfiguratsiyalari\n\n"
                "💬 Ushbu skriptlar to'plami kerakmi? Kommentga «KOD» deb yozing, Direct'ga to'liq yuboramiz! 📥👇\n\n"
                "🚀 Profilimizda har kuni eng zo'r IT sirlari! Obuna: @coder_bahrom"
            ),
            (
                "🔥 Dasturchilar ishini 5 baravar tezlashtiruvchi Terminal vositalari! 🛠⚡️\n\n"
                "Rutin ishlarga vaqtingizni sarflamang, terminal skriptlari hammasini avtomatlashtiradi:\n\n"
                "✅ Kuchli CLI qidiruv va ma'lumot tahlili\n"
                "✅ Birgina buyruq bilan murakkab jarayonlarni boshqarish\n"
                "✅ Linux, Mac va Windows uchun to'liq mos\n\n"
                "👇 Siz qaysi biridan ko'proq foydalanasiz? Terminal-mi yoki Sichqoncha? Fikringizni kommentda yozing!\n"
                "💬 Skript kerak bo'lsa kommentga «KOD» deb yozing, Direct'ga yuboramiz!\n\n"
                "Kanalimiz: @coder_bahrom"
            ),
            (
                "🚀 Klaviaturadan qo'lni uzmasdan ishlash! Terminal dasturchi sirlari ⚡️💻\n\n"
                "Noldan kod yozib o'tirmang — tayyor ochiq kodli vositalar orqali professional darajaga chiqing:\n\n"
                "✅ Zsh, Bash va CLI maxsus aliaslari\n"
                "✅ Serverlarni 1 soniyada monitoring qilish\n"
                "✅ Git va GitHub buyruqlarini avtomatlashtirish\n\n"
                "💬 Skriptlarni olish uchun kommentga «KOD» deb yozing, Direct'ga jo'natamiz! 📥\n"
                "💎 Do'stingizga ham yuboring, u ham vaqtini tejashni o'rgansin! Obuna: @coder_bahrom"
            ),
        ],
        "caption": (
            "⚡️ Hech kim bilmaydigan eng kuchli Terminal xaklari va CLI skriptlar! 💻🔥\n\n"
            "Sichqonchani unuting — dasturchilar va IT mutaxassislari hamma vazifani terminalda 5 baravar tezroq bajaradi!\n\n"
            "✅ Ishni 10x tezlashtiruvchi CLI buyruqlar\n"
            "✅ Tezkor navigatsiya va fayllarni boshqarish\n"
            "✅ Zsh, Bash va Neovim ajoyib konfiguratsiyalari\n\n"
            "💬 Ushbu skriptlar to'plami kerakmi? Kommentga «KOD» deb yozing, Direct'ga to'liq yuboramiz! 📥👇\n\n"
            "🚀 Profilimizda har kuni eng zo'r IT sirlari! Obuna: @coder_bahrom"
        ),
        "hashtags": ["terminal", "linux", "dasturchi", "bash", "codinglife", "ituz", "coder"],
    },
    {
        "time": "12:00",
        "label": "🔥 Kunduzgi Rek (12:00)",
        "niche": "Python Avtomatlashtirish & Cyber Skriptlar",
        "search_tags": [
            "terminalaesthetic", "pythoncode", "scripting", "networksecurity", "cybersecuritytools", "infosec", "automation",
        ],
        "captions": [
            (
                "🐍 Python da vazifalarni avtomatlashtiruvchi ajoyib skriptlar! ⚡️💻\n\n"
                "Soatlab qilinadigan ishlarni bir necha qator kod orqali 5 soniyada bajaring:\n"
                "✅ Internetdan ma'lumotlarni avtomatik yig'ish (Scraping)\n"
                "✅ Fayllarni saralash va avtomatik hisobotlar\n"
                "✅ Tarmoq va xavfsizlik monitoringi skriptlari\n\n"
                "🚀 Python dasturlash va avtomatlashtirish sirlari: @coder_bahrom"
            ),
            (
                "🤖 Rutin ishlarni kodga topshiring! Python avtomatlashtirish xaklari 🔥\n\n"
                "Dasturchi va IT mutaxassislari uchun tayyor vositalar:\n"
                "✅ API bilan ishlash va ma'lumotlar tahlili\n"
                "✅ Telegram botlar orqali boshqariluvchi skriptlar\n"
                "✅ Ochiq kodli GitHub loyihalari\n\n"
                "💡 Profilimizga obuna bo'ling: @coder_bahrom"
            ),
        ],
        "caption": (
            "🐍 Python da vazifalarni avtomatlashtiruvchi ajoyib skriptlar! ⚡️💻\n\n"
            "Soatlab qilinadigan ishlarni bir necha qator kod orqali 5 soniyada bajaring:\n"
            "✅ Internetdan ma'lumotlarni avtomatik yig'ish (Scraping)\n"
            "✅ Fayllarni saralash va avtomatik hisobotlar\n"
            "✅ Tarmoq va xavfsizlik monitoringi skriptlari\n\n"
            "🚀 Python dasturlash va avtomatlashtirish sirlari: @coder_bahrom"
        ),
        "hashtags": ["python", "automation", "dasturlash", "ituzbekistan", "coding", "techtools"],
    },
    {
        "time": "18:30",
        "label": "🌆 Kechki Rek #1 (18:30)",
        "niche": "Top Dasturchi Vositalari & Dark Aesthetic Setup",
        "search_tags": [
            "codingsetup", "darkaesthetic", "desksetup", "mechanicalkeyboard", "workspacegoals", "cleanworkspace", "darkmode",
        ],
        "captions": [
            (
                "✨ Har bir dasturchining noutbukida bo'lishi kerak bo'lgan super vositalar! 💻🖤\n\n"
                "Ish muhitini (workflow) maksimal qulay va chiroyli qilish sirlari:\n"
                "✅ Dark aesthetic kod muhiti va mavzular\n"
                "✅ Foydali VS Code va terminal plaginlari\n"
                "✅ Ish unumdorligini (Productivity) oshiruvchi vositalar\n\n"
                "🔥 Eng so'nggi IT trendlar uchun bizga qo'shiling: @coder_bahrom"
            ),
            (
                "🖥 Matrix uslubidagi dasturchi muhiti va foydali vositalar! ⚡️\n\n"
                "Kod yozishni zavqli jarayonga aylantiruvchi yechimlar:\n"
                "✅ Kuchli terminal emulyatorlari va shriftlar\n"
                "✅ Tezkor navigatsiya va klaviatura kombinatsiyalari\n"
                "✅ Ochiq kodli zamonaviy IT asboblari\n\n"
                "🚀 Ko'proq IT sirlari: @coder_bahrom"
            ),
        ],
        "caption": (
            "✨ Har bir dasturchining noutbukida bo'lishi kerak bo'lgan super vositalar! 💻🖤\n\n"
            "Ish muhitini (workflow) maksimal qulay va chiroyli qilish sirlari:\n"
            "✅ Dark aesthetic kod muhiti va mavzular\n"
            "✅ Foydali VS Code va terminal plaginlari\n"
            "✅ Ish unumdorligini (Productivity) oshiruvchi vositalar\n\n"
            "🔥 Eng so'nggi IT trendlar uchun bizga qo'shiling: @coder_bahrom"
        ),
        "hashtags": ["developertools", "codingsetup", "dasturchi", "ituz", "coder", "programmer"],
    },
    {
        "time": "20:30",
        "label": "🌙 Kechki Rek #2 (20:30)",
        "niche": "Linux & Axborot Xavfsizligi Vositalari",
        "search_tags": [
            "kalilinux", "terminalaesthetic", "unixporn", "archlinux", "securitytools", "cyberdefense",
        ],
        "captions": [
            (
                "🛡 Linux va Axborot Xavfsizligi (Ethical Hacking) vositalari! ⚡️💻\n\n"
                "Xavfsizlik mutaxassislari va kiberxavfsizlikka qiziquvchilar uchun ochiq kodli vositalar:\n"
                "✅ Tarmoq tahlili va zaifliklarni aniqlash\n"
                "✅ Linux tizimlarida xavfsizlikni kuchaytirish\n"
                "✅ Ochiq manbali kiberhimoya vositalari\n\n"
                "⚠️ Ma'lumotlar faqat o'quv va himoyalanish maqsadida taqdim etiladi.\n"
                "Obuna bo'ling: @coder_bahrom"
            ),
            (
                "🔒 Tizimni xakerlardan himoya qilish uchun qaysi vositalar kerak? 🛡⚡️\n\n"
                "Ochiq kodli eng mashhur xavfsizlik skriptlari va vositalari:\n"
                "✅ Kali Linux va ochiq terminal asboblari\n"
                "✅ Zaif nuqtalarni topish va himoyalash\n"
                "✅ Kiberxavfsizlik bo'yicha amaliy bilimlar\n\n"
                "🚀 Kiberolam va IT yangiliklari: @coder_bahrom"
            ),
        ],
        "caption": (
            "🛡 Linux va Axborot Xavfsizligi (Ethical Hacking) vositalari! ⚡️💻\n\n"
            "Xavfsizlik mutaxassislari va kiberxavfsizlikka qiziquvchilar uchun ochiq kodli vositalar:\n"
            "✅ Tarmoq tahlili va zaifliklarni aniqlash\n"
            "✅ Linux tizimlarida xavfsizlikni kuchaytirish\n"
            "✅ Ochiq manbali kiberhimoya vositalari\n\n"
            "⚠️ Ma'lumotlar faqat o'quv va himoyalanish maqsadida taqdim etiladi.\n"
            "Obuna bo'ling: @coder_bahrom"
        ),
        "hashtags": ["cybersecurity", "linux", "kalilinux", "infosec", "dasturlash", "ituz", "xavfsizlik"],
    },
]

_LAST_AUTOPILOT_CHECK_TS: float = 0.0
_AUTOPILOT_LOCK = asyncio.Lock()
USED_CODES_FILE = Path("data/used_codes.json")


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
    """Load used video codes from persistent JSON file and SQLite KV."""
    codes: set[str] = set()
    if USED_CODES_FILE.exists():
        try:
            data = json.loads(USED_CODES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("codes"), list):
                codes.update(str(x) for x in data["codes"])
        except Exception:
            pass

    try:
        data = await db.get_kv("autopilot_used_codes", None)
        if isinstance(data, dict) and isinstance(data.get("codes"), list):
            codes.update(str(x) for x in data["codes"])
    except Exception:
        pass
    return codes


async def _mark_code_used(code: str) -> None:
    """Save code to both persistent JSON file and SQLite KV."""
    used = await _get_used_codes()
    used.add(code)
    trimmed = sorted(list(used))[-500:]
    payload = {"codes": trimmed, "updated_at": time.time()}

    try:
        await db.set_kv("autopilot_used_codes", payload)
    except Exception:
        pass

    try:
        USED_CODES_FILE.parent.mkdir(parents=True, exist_ok=True)
        USED_CODES_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


async def find_and_download_b2b_video(slot_index: int = 0) -> dict[str, Any]:
    """Search Instagram for a high-engagement, UNIQUE coding/web Reel, download and normalize to 9:16."""
    settings = await ig_direct.load_direct_settings()
    if not settings:
        raise RuntimeError("Instagram akkaunt ulanmagan (/iglogin orqali ulang).")

    used_codes = await _get_used_codes()
    spec = DAILY_SLOTS_SPEC[slot_index % len(DAILY_SLOTS_SPEC)]
    search_tags = spec["search_tags"]

    temp_dir = config.TEMP_ROOT / "autopilot_reels"
    temp_dir.mkdir(parents=True, exist_ok=True)

    def _search_and_download() -> dict[str, Any]:
        import random
        cl = ig_direct._create_client(settings)
        my_uid = str(cl.user_id or "")

        # 1. Fetch our own profile media to guard against duplicate videos by duration and code
        existing_durations: list[float] = []
        existing_codes: set[str] = set()
        try:
            my_medias = cl.user_medias_v1(my_uid, amount=50)
            for mm in my_medias:
                d = float(getattr(mm, "video_duration", 0) or 0)
                if d > 0:
                    existing_durations.append(d)
                c_code = str(getattr(mm, "code", "") or "")
                if c_code:
                    existing_codes.add(c_code)
            log.info("Profile duplicate guard: %d existing reels detected", len(existing_durations))
        except Exception as exc:
            log.warning("Could not fetch profile medias for duplicate guard: %s", exc)

        candidates: list[Any] = []
        tags_to_try = list(search_tags)
        random.shuffle(tags_to_try)

        for tag in tags_to_try:
            # Query top
            try:
                medias = cl.hashtag_medias_top_v1(tag, amount=20)
                for m in medias:
                    code = str(getattr(m, "code", "") or "")
                    dur = float(getattr(m, "video_duration", 0) or 0)
                    mtype = int(getattr(m, "media_type", 0) or 0)
                    owner_pk = str(getattr(getattr(m, "user", None), "pk", "") or "")
                    if (
                        mtype == 2
                        and 7.0 <= dur <= 19.5
                        and code
                        and code not in used_codes
                        and code not in existing_codes
                        and owner_pk != my_uid
                        and not any(abs(ed - dur) < 0.35 for ed in existing_durations)
                        and not is_unwanted_content(m)
                    ):
                        candidates.append(m)
            except Exception as exc:
                log.debug("hashtag top search %s warning: %s", tag, exc)

            # Query recent
            try:
                medias = cl.hashtag_medias_recent_v1(tag, amount=20)
                for m in medias:
                    code = str(getattr(m, "code", "") or "")
                    dur = float(getattr(m, "video_duration", 0) or 0)
                    mtype = int(getattr(m, "media_type", 0) or 0)
                    owner_pk = str(getattr(getattr(m, "user", None), "pk", "") or "")
                    if (
                        mtype == 2
                        and 7.0 <= dur <= 19.5
                        and code
                        and code not in used_codes
                        and code not in existing_codes
                        and owner_pk != my_uid
                        and not any(abs(ed - dur) < 0.35 for ed in existing_durations)
                        and not is_unwanted_content(m)
                    ):
                        candidates.append(m)
            except Exception:
                pass

            if len(candidates) >= 12:
                break

        if not candidates:
            raise RuntimeError("Mos va takrorlanmas video topilmadi, keyingi urinishda qayta qidiriladi.")

        # Deduplicate candidates by code
        unique_cands: dict[str, Any] = {}
        for m in candidates:
            c = str(getattr(m, "code", ""))
            if c and c not in unique_cands:
                unique_cands[c] = m
        cands_list = list(unique_cands.values())

        # Sort by engagement (views + likes * 10)
        cands_list.sort(
            key=lambda x: int(getattr(x, "play_count", 0) or 0) + int(getattr(x, "like_count", 0) or 0) * 10,
            reverse=True,
        )

        # Select candidates and verify WITH OPENCV FACE DETECTOR: 100% NO HUMAN FACES ALLOWED
        shuffled_pool = list(cands_list[:min(15, len(cands_list))])
        random.shuffle(shuffled_pool)

        chosen = None
        dl_path = None
        code = ""

        for candidate in shuffled_pool:
            cand_code = str(candidate.code)
            log.info("Downloading candidate %s to verify NO HUMAN FACES...", cand_code)
            cand_dl = Path(cl.video_download(candidate.pk, folder=temp_dir))
            has_face, face_reason = has_human_face_or_person(cand_dl)
            if has_face:
                log.warning("REJECTED candidate %s: %s (NO HUMAN FACES allowed)", cand_code, face_reason)
                cand_dl.unlink(missing_ok=True)
                used_codes.add(cand_code)
                continue

            # Candidate passed face & person check!
            log.info("APPROVED candidate %s: 100%% faceless terminal/screen/setup video!", cand_code)
            chosen = candidate
            code = cand_code
            dl_path = cand_dl
            break

        if not chosen or not dl_path or not dl_path.exists():
            raise RuntimeError("Topilgan barcha videolarda odam yuzi aniqlandi. Qayta qidiriladi.")

        norm_path = temp_dir / f"norm_{code}.mp4"
        wm_path = temp_dir / f"wm_{code}.png"
        hook_path = temp_dir / f"hook_{code}.png"
        cta_path = temp_dir / f"cta_{code}.png"

        # Draw clean, sleek Uzbek top & bottom banners (Visual only, NO AI voiceover - pure music preserved)
        try:
            from PIL import Image, ImageDraw, ImageFont

            def _font(size: int, bold: bool = False):
                cands = (
                    ["arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
                    if bold else
                    ["arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
                )
                for f in cands:
                    try:
                        return ImageFont.truetype(f, size)
                    except Exception:
                        continue
                return ImageFont.load_default()

            font_title = _font(34, True)
            font_sub = _font(24)
            font_cta = _font(30, True)
            font_brand = _font(26)
            font_wm = _font(26, True)
            font_credit = _font(20)

            W, H = 1080, 1920
            hook_map = [
                ("HECH KIM BILMAYDIGAN", "GITHUB SIRLARI"),
                ("ISHNI 5X TEZLASHTIRUVCHI", "TERMINAL XAKLARI"),
                ("5 SONIYADA ISHLAYDIGAN", "PYTHON SKRIPT"),
                ("HAR BIR DASTURCHIGA", "KERAKLI VOSITA"),
                ("LINUX XAVFSIZLIK", "VOSITALARI"),
            ]
            h_small, h_big = hook_map[slot_index % len(hook_map)]
            font_hs = _font(34, True)
            font_hb = _font(58, True)

            # Layer 1: watermark (whole video) - small IG logo + @coder_bahrom, author credit
            wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(wm)
            lx, ly, ls = 44, 232, 40
            name_w = d.textlength("@coder_bahrom", font=font_wm)
            d.rounded_rectangle([lx - 10, ly - 8, lx + ls + 22 + name_w, ly + ls + 8], radius=24, fill=(0, 0, 0, 90))
            d.rounded_rectangle([lx, ly, lx + ls, ly + ls], radius=11, outline=(255, 255, 255, 230), width=3)
            d.ellipse([lx + 11, ly + 11, lx + ls - 11, ly + ls - 11], outline=(255, 255, 255, 230), width=3)
            d.ellipse([lx + ls - 11, ly + 6, lx + ls - 6, ly + 11], fill=(255, 255, 255, 230))
            d.text((lx + ls + 12, ly + ls // 2), "@coder_bahrom", font=font_wm, fill=(255, 255, 255, 235), anchor="lm")
            author = str(getattr(getattr(chosen, "user", None), "username", "") or "").strip()
            if author:
                credit = f"video: @{author}"
                tw = d.textlength(credit, font=font_credit)
                d.rounded_rectangle([W - 44 - tw - 24, 238, W - 44, 274], radius=18, fill=(0, 0, 0, 90))
                d.text((W - 44 - 12, 256), credit, font=font_credit, fill=(230, 230, 230, 220), anchor="rm")
            wm.save(wm_path)

            # Layer 2: hook (first 2.5s) - frameless glass pill
            hk = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(hk)
            bw = max(d.textlength(h_small, font=font_hs), d.textlength(h_big, font=font_hb)) + 90
            d.rounded_rectangle([(W - bw) / 2, 560, (W + bw) / 2, 760], radius=36, fill=(10, 12, 20, 175))
            d.text((W / 2, 615), h_small, font=font_hs, fill=(255, 255, 255, 255), anchor="mm")
            d.text((W / 2, 692), h_big, font=font_hb, fill=(0, 225, 255, 255), anchor="mm")
            hk.save(hook_path)

            # Layer 3: CTA (last 3s)
            ct = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(ct)
            l1, l2 = "Kommentga \u00abKOD\u00bb deb yozing", "skriptni Direct'ga yuboramiz"
            bw = max(d.textlength(l1, font=font_cta), d.textlength(l2, font=font_brand)) + 90
            d.rounded_rectangle([(W - bw) / 2, 1300, (W + bw) / 2, 1440], radius=36, fill=(10, 12, 20, 185))
            d.text((W / 2, 1345), l1, font=font_cta, fill=(255, 210, 70, 255), anchor="mm")
            d.text((W / 2, 1400), l2, font=font_brand, fill=(255, 255, 255, 240), anchor="mm")
            ct.save(cta_path)
        except Exception as exc:
            log.warning("overlay banner warning: %s", exc)

        # Composite video: keep original viral music track at full 100% volume, NO AI VOICEOVER
        vdur = min(float(getattr(chosen, "video_duration", 0) or 28.0), 28.0)
        cta_start = max(vdur - 3.0, 3.0)
        if wm_path.exists() and hook_path.exists() and cta_path.exists():
            ff_args = [
                "ffmpeg", "-y", "-v", "quiet",
                "-i", str(dl_path),
                "-loop", "1", "-t", f"{vdur:.2f}", "-i", str(wm_path),
                "-loop", "1", "-t", f"{vdur:.2f}", "-i", str(hook_path),
                "-loop", "1", "-t", f"{vdur:.2f}", "-i", str(cta_path),
                "-filter_complex",
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30[base];"
                "[2:v]format=rgba,fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st=2.2:d=0.4:alpha=1[hk];"
                f"[3:v]format=rgba,fade=t=in:st={cta_start:.2f}:d=0.4:alpha=1[ct];"
                "[base][1:v]overlay=0:0:shortest=1[v1];"
                "[v1][hk]overlay=0:0:shortest=1[v2];"
                "[v2][ct]overlay=0:0:shortest=1[vout]",
                "-map", "[vout]",
                "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{vdur:.2f}",
                str(norm_path),
            ]
        else:
            ff_args = [
                "ffmpeg", "-y", "-v", "quiet",
                "-i", str(dl_path),
                "-t", "28",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(norm_path),
            ]

        subprocess.run(ff_args, check=True, timeout=180)
        dl_path.unlink(missing_ok=True)
        for p in (wm_path, hook_path, cta_path):
            p.unlink(missing_ok=True)

        caption_pool = spec.get("captions", [spec.get("caption", "")])
        chosen_caption = random.choice(caption_pool)

        return {
            "path": norm_path,
            "code": code,
            "duration": min(28.0, float(getattr(chosen, "video_duration", 15.0) or 15.0)),
            "caption": chosen_caption,
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
