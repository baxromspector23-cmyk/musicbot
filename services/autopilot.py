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
        "search_tags": [
            "restaurantwebsite", "cafemenu", "foodapp", "restaurantmenu", "digitalmenu",
            "restauranttech", "webdesign", "uidesign", "webdevelopment", "uxdesign",
            "foodordering", "creativeagency", "cafedesign", "modernwebsite",
        ],
        "captions": [
            (
                "🍽 Restoran yoki kafengiz uchun mijozlarni jalb qiluvchi Zamonaviy Sayt va Onlayn Menyu! ✨\n\n"
                "Mijozlaringiz telefon orqali menyuni ko'rib, birgina tugma bilan buyurtma berishini xohlaysizmi?\n"
                "✅ Interaktiv 3D / foto menyu va QR-kod tizimi\n"
                "✅ Yetkazib berish (Dostavka) va bron qilish funksiyasi\n"
                "✅ Telegram bot bilan to'liq integratsiya\n\n"
                "📈 Restoraningiz savdosini 2 barobarga oshiring!\n"
                "📩 Zakaz berish uchun Direct (DM) ga «MENYU» yoki «ZAKAZ» deb yozing!"
            ),
            (
                "☕️ Qahvaxona yoki restoraningizga yangi mijozlar oqimini oshirmoqchimisiz? 🚀\n\n"
                "Zamonaviy mehmonlar ofitsiantni kutishni yoqtirishmaydi. Smart Onlayn Menyu tizimi orqali:\n"
                "✅ Stol ustidagi QR-kod orqali tezkor buyurtma\n"
                "✅ O'zbek, rus va ingliz tillarida qulay menyu\n"
                "✅ Admin panel va kunlik tushumlar hisoboti\n\n"
                "💼 Muassasangiz nufuzini yangi darajaga olib chiqing!\n"
                "📩 Individual tizim uchun Direct (DM) ga «RESTORAN» deb yozing!"
            ),
            (
                "📲 Telefon orqali 1 daqiqada zakaz qabul qiluvchi Zamonaviy Menyu Sayti! 🍕\n\n"
                "Yetkazib berish xizmatini uchinchi tomon agregatorlarisiz, to'g'ridan-to'g'ri o'zingiz boshqaring:\n"
                "✅ Click, Payme va naqd to'lov tizimlari\n"
                "✅ Har bir buyurtma darhol Telegram guruhingizga tushadi\n"
                "✅ Oylik komissiyasiz, 100% o'zingizga tegishli tizim\n\n"
                "🔥 Buyurtma berish uchun Direct (DM) ga «ZAKAZ» deb yozing!"
            ),
            (
                "🍔 Taomlaringizni eng jozibador ko'rinishda taqdim etuvchi zamonaviy veb-sayt! ✨\n\n"
                "Nafis dizayn va maksimal tezlik bilan ishlovchi yechim:\n"
                "✅ Animatsiyali taomlar kartochkasi\n"
                "✅ Mijozlar uchun sodiq bonuslar tizimi\n"
                "✅ Barcha smartfonlarga moslashuvchan interfeys\n\n"
                "📩 Restoraningiz uchun individual saytga buyurtma bering: Direct ga yozing!"
            ),
        ],
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
        "search_tags": [
            "corporatedesign", "businesswebsite", "landingpage", "webagency", "branddesign",
            "webdeveloper", "frontend", "minimaldesign", "websitedesigning", "fullstackdeveloper",
            "cleanwebsite", "modernui", "corporatesite",
        ],
        "captions": [
            (
                "💼 Kompaniyangiz nufuzini va savdosini oshiruvchi Zamonaviy Veb-Sayt kerakmi? 🚀\n\n"
                "Har bir loyihani 0 dan boshlab, zamonaviy dizayn va tezkor kod asosida tayyorlab beramiz:\n"
                "✅ Kompaniya va xizmat ko'rsatish bizneslari uchun Korporativ Sayt\n"
                "✅ Yuqori konversiyali Landing Page\n"
                "✅ Google va Instagram qidiruviga (SEO) moslashtirilgan tizim\n\n"
                "🤝 Biznesingizni onlaynda yangi bosqichga olib chiqing!\n"
                "📩 Loyiha buyurtma qilish uchun Direct (DM) ga «ZAKAZ» deb yozing!"
            ),
            (
                "🌐 Har qanday nufuzli biznesning asosi — bu professional veb-sayt! 🏆\n\n"
                "Mijozlaringiz sizni qidirganda professional ko'rinishga ega bo'ling:\n"
                "✅ Ishonch uyg'otuvchi premium vizual uslub\n"
                "✅ Xizmatlar, portfolio va mijozlar fikrlari bo'limi\n"
                "✅ Direct va Telegram orqali avtomatik arizalar qabul qilish\n\n"
                "💼 Kompaniyangiz uchun individual taklif oling:\n"
                "📩 Direct (DM) ga «SAYT» deb yozing!"
            ),
            (
                "📈 Reklamadan kelgan mijozlarni 3 barobar ko'proq ushlab qoluvchi Landing Page! ⚡\n\n"
                "Oddiy sahifalar mijozni yo'qotadi. Biz yaratgan saytlar esa sotuvga yo'naltirilgan:\n"
                "✅ Kuchli marketing tuzilmasi va chaqiruv tugmalari (CTA)\n"
                "✅ 0.4 soniyalik maksimal yuklanish tezligi\n"
                "✅ CRM tizimlariga to'g'ridan-to'g'ri ulanish\n\n"
                "📩 Bepul konsultatsiya olish uchun Direct ga yozing!"
            ),
            (
                "💎 Biznesingiz brendini bozorda yetakchiga aylantiruvchi veb-yechimlar! ✨\n\n"
                "Kompaniya ko'lamini ko'rsatuvchi zamonaviy IT yechimlar yaratamiz:\n"
                "✅ Eksklyuziv dizayn, shablonlarsiz toza kod\n"
                "✅ Domen, xosting va xavfsizlik (SSL) to'liq sozlanadi\n"
                "✅ 1 yillik bepul texnik yordam va kafolat\n\n"
                "🤝 Hamkorlik uchun Direct (DM) ga yozing!"
            ),
        ],
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
        "search_tags": [
            "uiux", "interactivedesign", "webdevelopment", "threejs", "frontenddeveloper",
            "3ddesign", "awwwards", "creativecoding", "cssanimation", "javascript",
            "uidesign", "webdeveloper", "frontend",
        ],
        "captions": [
            (
                "✨ Biznesingiz uchun eng so'nggi trenddagi Zamonaviy va Interaktiv Veb-Sayt! 🚀\n\n"
                "Oddiy saytlar mijozni ushlab qololmaydi. Biz yaratadigan saytlar esa mijozni bir ko'rishda hayratda qoldiradi va buyurtma berishga undaydi:\n"
                "✅ 3D elementlar va silliq interaktiv animatsiyalar\n"
                "✅ Telefon va noutbuklarda maksimal yuklanish tezligi (< 0.5s)\n"
                "✅ Onlayn to'lov va to'liq avtomatlashtirilgan CRM tizimi\n\n"
                "💼 Saytingiz siz uchun 24/7 yangi mijozlar olib kelsin!\n"
                "📩 Loyihangizni boshlash uchun Direct (DM) ga «SAYT» deb yozing!"
            ),
            (
                "⚡️ Sayt shunchaki matn emas — bu mijoz bilan hissiy aloqa! 🎨\n\n"
                "Awwwards darajasidagi interaktiv UI/UX dizayn va silliq mikro-animatsiyalar:\n"
                "✅ Premium vizual effektlar\n"
                "✅ Foydalanuvchi uchun nihoyatda qulay navigatsiya\n"
                "✅ Raqobatchilaringizdan darhol ajralib turish kafolati\n\n"
                "🔥 O'z loyihangizni boshlash uchun Direct ga «UIUX» deb yozing!"
            ),
            (
                "💻 Zamonaviy kod va chiroyli animatsiyalar uyg'unligi! 🛠\n\n"
                "React, Vue va ilg'or frontend texnologiyalarida yaratilgan loyihalar:\n"
                "✅ Har bir piksel aniq va qat'iy tekshiriladi\n"
                "✅ Har qanday ekranda mukammal moslashuv (Responsive)\n"
                "✅ Tez, xavfsiz va kelajakka mo'ljallangan arxitektura\n\n"
                "📩 Sayt yaratish bo'yicha maslahat olish uchun Direct ga yozing!"
            ),
            (
                "🎯 Saytingiz 3 soniya ichida mijozda WOW-effekt uyg'otishi kerak! ✨\n\n"
                "Nafis interfeyslar va kuchli dasturlash asosida tayyorlangan yechimlar:\n"
                "✅ Silliq scroll va dinamik kartochkalar\n"
                "✅ Mijoz e'tiborini kerakli tugmaga yo'naltiruvchi dizayn\n"
                "✅ Katta savdolarga zamin yaratuvchi zamonaviy IT mahsulot\n\n"
                "📩 Zakaz berish uchun Direct (DM) ga yozing!"
            ),
        ],
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
        "search_tags": [
            "ecommercewebsite", "onlineshopdesign", "shopifydesign", "webshop", "appdesign",
            "productdesign", "mobileappdesign", "cleanui", "ecommercedeveloper", "uxdesign",
            "uidesign", "webdesign",
        ],
        "captions": [
            (
                "🛍 Do'koningiz savdosini 24/7 to'xtovsiz ishlaydigan Onlayn Do'kon bilan oshiring! ✨\n\n"
                "Mijozlar mahsulotlaringizni qulay katalogda ko'rib, darhol xarid qilishlari uchun zamonaviy yechim:\n"
                "✅ Barcha telefon va kompyuterlarga moslashuvchan dizayn\n"
                "✅ Click / Payme to'lov tizimlari va savatcha funksiyasi\n"
                "✅ Buyurtmalarni Telegram orqali tezkor qabul qilish\n\n"
                "📈 Savdoingizni avtomatlashtirish vaqti keldi!\n"
                "📩 Zakaz berish uchun Direct (DM) ga «DOKON» yoki «ZAKAZ» deb yozing!"
            ),
            (
                "📦 Mahsulotlaringizni butun O'zbekiston bo'ylab onlayn soting! 🚚\n\n"
                "Do'koningiz faqat bitta manzilda cheklanib qolmasin:\n"
                "✅ Qulay filtrlar, o'lchamlar va ranglar tanlash imkoniyati\n"
                "✅ Bir zumda to'lov va xarid chekini yuborish\n"
                "✅ Ombor hisobi va qoldiqlar nazorati\n\n"
                "💼 O'z onlayn do'koningizni ishga tushiring:\n"
                "📩 Hoziroq Direct (DM) ga yozing!"
            ),
            (
                "🛒 Savdoni Instagram Direct dan to'liq avtomatlashtirilgan tizimga o'tkazing! ⚡\n\n"
                "Har bir mijozga narx yozib o'tirmang — sayt barchasini o'zi bajaradi:\n"
                "✅ 24 soat avtomatik buyurtma qabul qilish\n"
                "✅ Telefon raqami va manzilni xatosiz kiritish formasi\n"
                "✅ Yetkazib beruvchilarga tayyor marshrut ma'lumotlari\n\n"
                "📩 Onlayn do'kon ochish uchun Direct ga «SAVDO» deb yozing!"
            ),
            (
                "📱 Zamonaviy mobil internet-do'kon — mijozlar cho'ntagidagi biznesingiz! 💎\n\n"
                "Ilovadek tez ochiladigan PWA veb-do'konlar:\n"
                "✅ Tezkor savatcha va buyurtma rasmiylashtirish\n"
                "✅ Chegirma promokodlari va aksiyalar bo'limi\n"
                "✅ Mijozlar uchun shaxsiy kabinet\n\n"
                "📩 Loyiha yaratish uchun Direct (DM) ga yozing!"
            ),
        ],
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
        "search_tags": [
            "pythonprogramming", "softwaredeveloper", "backenddeveloper", "codinganimation",
            "devcommunity", "automation", "techtrends", "programmerlife", "codinglife",
            "telegrambot", "webdeveloper", "fullstack",
        ],
        "captions": [
            (
                "🤖 Mijozlardan 24/7 zakaz qabul qiluvchi Zamonaviy Sayt va Telegram Bot! ⚡\n\n"
                "Kod qatorlaridan — tayyor daromad keltiruvchi biznes tizimgacha:\n"
                "✅ Restoran, do'kon va kompaniyalar uchun Maxsus Telegram Botlar\n"
                "✅ Telegram Mini-App (WebApp) onlayn katalog va buyurtma tizimi\n"
                "✅ Mijozlar bazasi va avtomat hisobotlar\n\n"
                "💼 Biznesingiz uchun individual loyiha kerakmi?\n"
                "📩 Hoziroq Direct (DM) ga «ZAKAZ» deb yozing — bepul maslahat beramiz!"
            ),
            (
                "⚡️ Xodimlarsiz 24/7 ishlovchi aqlli Telegram Bot va Mini-App! 🚀\n\n"
                "Mijozlaringiz bilan avtomatik muloqot va tezkor savdo tizimi:\n"
                "✅ Telegram ichida ochiluvchi to'liq vizual katalog (Mini App)\n"
                "✅ Click, Payme va Uzum to'lovlari\n"
                "✅ Buyurtmalarni avtomatik tarzda kuryer yoki omborga yo'naltirish\n\n"
                "📩 Bot yaratish uchun Direct (DM) ga «BOT» deb yozing!"
            ),
            (
                "📈 Biznes jarayonlarini avtomatlashtirib, vaqtingiz va pulingizni tejang! 💻\n\n"
                "Muntazam qilinadigan takroriy ishlarni botlarga topshiring:\n"
                "✅ Mijozlar bazasi (CRM) va avtomatik eslatmalar\n"
                "✅ Xodimlar va filiallar faoliyatini boshqarish paneli\n"
                "✅ Excel hisobotlar va tahlillar bir tugmada\n\n"
                "💼 O'z tizimingizni yarating: Direct ga yozing!"
            ),
            (
                "🎯 Telegram Mini-App (WebApp) — zamonaviy biznesning eng kuchli quroli! 🔥\n\n"
                "Ilova yuklab olishga hojat yo'q, Telegram ichida 1 soniyada ochiladi:\n"
                "✅ Ultra-zamonaviy interfeys va qulay buyurtma\n"
                "✅ Har bir mijoz uchun shaxsiy bonuslar va ballar\n"
                "✅ Doimiy mijozlar sonini keskin oshirish imkoniyati\n\n"
                "📩 Loyihangizni boshlash uchun Direct ga «WEBAPP» deb yozing!"
            ),
        ],
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
                        and 6.0 <= dur <= 38.0
                        and code
                        and code not in used_codes
                        and code not in existing_codes
                        and owner_pk != my_uid
                        and not any(abs(ed - dur) < 0.35 for ed in existing_durations)
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
                        and 6.0 <= dur <= 38.0
                        and code
                        and code not in used_codes
                        and code not in existing_codes
                        and owner_pk != my_uid
                        and not any(abs(ed - dur) < 0.35 for ed in existing_durations)
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

        # Pick randomly from top 8 to guarantee viral quality while NEVER repeating the same video
        top_pool = cands_list[:min(8, len(cands_list))]
        chosen = random.choice(top_pool)
        code = str(chosen.code)
        dl_path = Path(cl.video_download(chosen.pk, folder=temp_dir))
        norm_path = temp_dir / f"norm_{code}.mp4"
        overlay_path = temp_dir / f"overlay_{code}.png"

        # Draw clean, sleek Uzbek top & bottom banners (Visual only, NO AI voiceover - pure music preserved)
        try:
            from PIL import Image, ImageDraw, ImageFont
            font_title = ImageFont.truetype("arialbd.ttf", 34) if os.name == "nt" else ImageFont.load_default()
            font_sub = ImageFont.truetype("arial.ttf", 24) if os.name == "nt" else ImageFont.load_default()
            font_cta = ImageFont.truetype("arialbd.ttf", 30) if os.name == "nt" else ImageFont.load_default()
            font_brand = ImageFont.truetype("arial.ttf", 26) if os.name == "nt" else ImageFont.load_default()

            overlay_img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay_img)

            # Top Uzbek banner (sleek dark glass effect)
            top_box = [90, 370, 990, 525]
            odraw.rounded_rectangle(top_box, radius=20, fill=(12, 15, 24, 210), outline=(0, 220, 255, 230), width=3)
            odraw.text((130, 395), spec["niche"][:32].upper(), font=font_title, fill=(255, 255, 255))
            odraw.text((145, 460), "Individual dizayn va tezkor dasturlash", font=font_sub, fill=(0, 230, 255))

            # Bottom Uzbek CTA (sleek golden pill badge)
            bot_box = [90, 1675, 990, 1825]
            odraw.rounded_rectangle(bot_box, radius=20, fill=(12, 15, 24, 210), outline=(255, 195, 45, 230), width=3)
            odraw.text((140, 1698), "Buyurtma berish uchun Direct ga yozing", font=font_cta, fill=(255, 220, 90))
            odraw.text((380, 1752), "@coder_bahrom", font=font_brand, fill=(255, 255, 255))

            overlay_img.save(overlay_path)
        except Exception as exc:
            log.warning("overlay banner warning: %s", exc)

        # Composite video: keep original viral music track at full 100% volume, NO AI VOICEOVER
        if overlay_path.exists():
            ff_args = [
                "ffmpeg", "-y", "-v", "quiet",
                "-i", str(dl_path),
                "-i", str(overlay_path),
                "-filter_complex",
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30[base];"
                "[base][1:v]overlay=0:0[vout]",
                "-map", "[vout]",
                "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-t", "28",
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
        overlay_path.unlink(missing_ok=True)

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
