# Shaxsiy Telegram Musiqa Tahrirlash va Instagram Reels Auto-Poster Boti

Ushbu loyiha bitta foydalanuvchi (bot egasi) uchun mo'ljallangan ishlab chiqarish (production) sifatidagi ko'p funksiyali Telegram botidir:
1. **Musiqa va audio fayllarni professional darajada tahrirlash** (FFmpeg yordamida kesish, tezlik, pitch, tovush balandligi, formatlar konvertatsiyasi, teglarni tahrirlash va audio ajratish).
2. **Instagram Reels Auto-Poster** (Videolarni 9:16 formatga keltirish, sun'iy intellekt orqali tavsif va hashtaglar yozish, obunachilar eng faol bo'lgan vaqtni tahlil qilib rejalashtirish hamda rasmiy Meta Graph API orqali avtomatlashtirilgan tarzda serverdan chop etish).

Loyiha Render platformasida Docker Web Service sifatida ishlaydi va 512 MB RAM / 0.1 CPU kabi minimal resurslarda barqaror ishlashga to'liq moslashtirilgan.

---

## 🚀 Imkoniyatlar

### 🎵 1. Musiqa va Audio Tahrirlash
- **Kesish:** `0:30 1:45` yoki `30 105` formatida aniq kesish (mp3/m4a/flac/wav formatlarida stream-copy orqali sifat yo'qotilmaydi).
- **Fade:** Boshidan sekin kuchayish (Fade In), oxirida pasayish (Fade Out) yoki har ikkalasi.
- **Tezlik:** 0.5x dan 2.0x gacha tezlashtirish yoki sekinlashtirish (tonallikni saqlagan holda).
- **Pitch (Tonallik):** -12 dan +12 yarim ton oralig'ida tonallikni o'zgartirish (davomiylik o'zgarmaydi).
- **Ovoz (Volume):** Foizda kuchaytirish/pasaytirish va EBU R128 bo'yicha normallashtirish (-14 LUFS, -16 LUFS, -9 LUFS).
- **Birlashtirish (Merge):** 10 tagacha trekni birlashtirish (oddiy ulash yoki silliq crossfade bilan).
- **Konvertatsiya:** MP3, M4A, OGG, WAV, FLAC formatlariga (128k–320k bitrate tanlovi bilan).
- **Audio ajratish:** Video fayllardan audio trekni ajratib olish.
- **Teglar va Muqova:** Qo'shiq nomi, ijrochi, albom va albom muqovasini o'rnatish.
- **Ringtone:** Android (MP3) va iPhone (M4R) uchun moslashtirilgan ringtone yaratish.
- **Orqaga qaytarish:** Oxirgi tahrir qadamini bekor qilish (Undo) imkoniyati.

### 📸 2. Instagram Reels Auto-Poster
- **Video Intake & Tekshiruv:** 20 MB gacha bo'lgan videolarni qabul qiladi, ffprobe orqali parametrlarini tekshiradi (kamida 3 soniya, eng ko'pi bilan 15 daqiqa).
- **9:16 Moslashtirish:**
  - *Markazdan kesish (Crop 9:16)*
  - *Xira/qora fon bilan to'ldirish (Blurred Pad 1080x1920)*
  - *O'zgarishsiz qoldirish*
- **Hajm Nazorati (Bitrate Capping):** Telegram Bot API yuklab olish chekloviga (20 MB) mos ravishda tayyorlangan video `MAX_REEL_MB` (19 MB) dan oshmaydi.
- **Sun'iy Intellekt Tavsifi (AI Caption):** Gemini yoki Anthropic (Claude) orqali video kadrlaridan foydalangan holda qidiruv algoritmlari (SEO) uchun optimallashtirilgan, birinchi 120 belgisi qiziqtiruvchi (hook), 2-4 qatorli va eng ko'pi bilan 5 ta tegishli hashtagli tavsif tayyorlanadi.
- **Eng Maqbul Vaqtni Aniqlash (Best-Time Scheduling):**
  - Instagram hisobining obunachilar faolligi xaritasi (`online_followers`) silliqlanadi (0.25 / 0.50 / 0.25).
  - Tungi tinch soatlar (`QUIET_HOURS`, masalan 00:00–07:00) hisobga olinadi.
  - Kunlik postlar limiti (`MAX_POSTS_PER_DAY`) va postlar orasidagi masofa (`MIN_GAP_HOURS`, kamida 4 soat) tekshiriladi.
  - Organik 0–9 daqiqalik tebranish (jitter) qo'shiladi.
- **Tabiiy Tilda Vaqt Belgilash:** Foydalanuvchi ixtiyoriy vaqtni qo'lda kiritishi mumkin:
  - `"bugun 20:30"`, `"ertaga 19:00"`, `"+2h"`, `"30 daqiqadan keyin"`, `"25.10 21:00"`.
- **Navbat va Xavfsizlik:** Postlar SQLite yoki PostgreSQL bazasida saqlanadi. Atomar qulflash (`locked_until`) orqali bir xil post ikki marta joylanishi to'liq oldini olinadi.
- **Rasmiy Meta Graph API Resumable Upload:** Videoni to'g'ridan-to'g'ri `rupload.facebook.com` ga bo'laklab (stream) yuklaydi va statusini tekshirib boradi.
- **Eslatma va Statistika:** Post joylanishidan 10 daqiqa oldin eslatma xabari, joylangandan 24 soat o'tgach esa ko'rishlar, layklar, qamrov va izohlar bo'yicha tahliliy hisobot yuboradi.

---

## 🛠 Meta Developer (Instagram API) Sozlash

Instagram API orqali avtomatik joylash rasmiy Meta Graph API orqali amalga oshiriladi:

### 1. Akkaunt Talablari
1. Instagram akkauntingizni **Professional (Creator yoki Business)** turiga o'tkazing.
2. Instagram akkauntingizni Facebook sahifangizga (Facebook Page) ulang.

### 2. Meta for Developers ilovasini yaratish
1. [Meta for Developers](https://developers.facebook.com) portaliga kiring.
2. **My Apps > Create App** tugmasini bosing va ilova turini **Business** deb tanlang.
3. Ilovaga **Instagram Graph API** mahsulotini qo'shing.
4. **App Settings > Basic** bo'limidan `App ID` va `App Secret` qiymatlarini nusxalab oling.

### 3. Ruxsatlar (Scopes) va Token olish
Ilovangizda quyidagi ruxsatlar (permissions) faollashtirilgan bo'lishi kerak:
- `instagram_basic`
- `instagram_content_publish`
- `instagram_manage_insights`
- `pages_show_list`
- `pages_read_engagement`

**Graph API Explorer** vositasi orqali User Token yarating va uni 60 kunlik Long-Lived tokenga almashtiring:
```
GET https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id={app-id}&client_secret={app-secret}&fb_exchange_token={short-lived-token}
```
Olingan tokenni `IG_ACCESS_TOKEN` o'zgaruvchisiga kiriting. Bot token muddati tugashiga 10 kun qolganda uni avtomatik ravishda yangilab boradi!

---

## 🗄 Ma'lumotlar Bazasi (Database)

- **Lokal sinovlar:** Sukut bo'yicha SQLite (`sqlite+aiosqlite:///./data.db`) ishlatiladi.
- **Render (Production):** Render'ning bepul instanslari fayl tizimini qayta yuklanishda tozalashi sababli, bepul tashqi PostgreSQL bazasidan foydalanish tavsiya etiladi:
  - [Neon.tech](https://neon.tech) (bepul serverless Postgres)
  - [Supabase](https://supabase.com)
  - `DATABASE_URL` parametrini quyidagi shaklda o'rnating:
    `postgresql+asyncpg://foydalanuvchi:parol@ep-host.neon.tech/neondb?ssl=require`

---

## 🌐 Render'ga Joylash (Deployment)

Loyihada Render uchun tayyor `render.yaml` Blueprints fayli mavjud:

1. Ushbu loyihani GitHub repozitoriysiga yuklang.
2. [Render.com](https://render.com) ga kiring va **Blueprints > New Blueprint Instance** tanlang.
3. GitHub repozitoriyangizni ulang. Render `render.yaml` orqali Docker xizmatini sozlaydi.
4. Render Dashboard'dagi **Environment** bo'limida quyidagi o'zgaruvchilarni to'ldiring:
   - `BOT_TOKEN`: Telegram bot tokeni ([@BotFather](https://t.me/BotFather) dan).
   - `OWNER_ID`: Sizning raqamli Telegram ID raqamingiz ([@userinfobot](https://t.me/userinfobot) dan).
   - `IG_USER_ID`: Professional Instagram hisob raqamingiz.
   - `IG_ACCESS_TOKEN`: Yuqorida olingan Meta API tokeni.
   - `IG_APP_ID` va `IG_APP_SECRET`: Meta App kalitlari.
   - `TOKEN_ENC_KEY`: 32-baytlik Fernet kaliti (boshlang'ich bo'sh qoldirilsa bot o'zi hosil qiladi).
   - `GEMINI_API_KEY` yoki `ANTHROPIC_API_KEY`: AI tavsif yozish uchun kalit.
   - `TICK_SECRET`: Uyg'otuvchi so'rovlar uchun maxfiy kalit.
   - `DATABASE_URL`: Neon / Supabase Postgres ulanish manzili.

### ⏰ Bepul Render instansini uyg'otish va rejalashtiruvchi (Cron)
Render Free instansi 15 daqiqa murojaat bo'lmasa uyqu rejimiga o'tadi. Rejalashtirilgan postlar o'z vaqtida chiqishi uchun [cron-job.org](https://cron-job.org) yoki [UptimeRobot](https://uptimerobot.com) dan bepul foydalaning:
- **URL:** `https://<sizning-ilovangiz>.onrender.com/tick?secret=<TICK_SECRET>`
- **Davriyligi:** Har 1 yoki 2 daqiqada bir marta (GET yoki POST).
Bu instansni uyg'oq ushlab turadi va kelgan vaqti bo'yicha postlarni darhol Instagram'ga chop etadi.

---

## 📱 Bot Buyruqlari

- `/start` — Botni ishga tushirish va asosiy menyu
- `/help` — Imkoniyatlar va yordam qo'llanmasi
- `/new` — Yangi sessiya ochish (avvalgi tahrirlarni tozalash)
- `/cancel` — Joriy amalni bekor qilish
- `/reel` — Instagram Reels uchun video yuklash va rejalashtirish
- `/queue` — Hozirda navbatda turgan postlar ro'yxati va ularni boshqarish
- `/history` — Oxirgi chop etilgan postlar tarixi va to'g'ridan-to'g'ri havolalar
- `/stats` — Joylangan postlarning 24 soatlik statistikasi (ko'rishlar, layklar, qamrov)
- `/igstatus` — Instagram akkaunt ma'lumotlari, token muddati va faollik tahlili holati

---

## 🧪 Avtomatlashtirilgan Sinovlar (Self-tests)

Barcha modullarning 100% to'g'ri ishlashini tekshirish uchun ikkita to'liq sinov dasturi mavjud:

```bash
# 1. Musiqa va audio tahrirlash sinovlari (kesish, pitch, tezlik, birlashtirish, konvertatsiya)
python -m scripts.selftest

# 2. Instagram Reels va AI moduli sinovlari (Mock Meta server, vaqt tahlili, 9:16 video prep, DB navbati)
python -m scripts.selftest_instagram
```
Ikkala test ham 100% muvaffaqiyat bilan o'tishi kafolatlangan.

---

## 📄 Litsenziya
Loyiha shaxsiy va professional foydalanish uchun ochiq. Barcha savollar va takliflar uchun bot egasiga murojaat qiling.
