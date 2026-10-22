# Musiqa tahrirlash Telegram boti

Shaxsiy (faqat egasi uchun) Telegram bot: audio va video fayllarni kesish, tahrirlash, konvertatsiya qilish. Render'da Docker Web Service sifatida, webhook rejimida ishlaydi.

## Imkoniyatlar

- **Kesish** — `0:30 1:45` formatida (mp3/m4a/flac/wav uchun sifat yo'qotilmaydi)
- **Fade** — fade in, fade out yoki ikkalasi
- **Tezlik** — 0.5x–2.0x, pitch saqlanadi yoki o'zgaradi
- **Pitch** — -12…+12 yarim ton, davomiylik o'zgarmaydi
- **Ovoz** — foiz bilan o'zgartirish va normallashtirish (-14 / -16 / -9 LUFS)
- **Birlashtirish** — 10 tagacha fayl, oddiy yoki crossfade
- **Konvertatsiya** — mp3, m4a, ogg, wav, flac (128–320k)
- **Audio ajratish** — videodan audio
- **Teglar** — nomi, ijrochi, albom, muqova
- **Ringtone** — Android (mp3) va iPhone (m4r)
- **Orqaga** (oxirgi qadamni bekor qilish) va **Tayyor**

## BOT_TOKEN va OWNER_ID olish

1. Telegram'da [@BotFather](https://t.me/BotFather) ga `/newbot` yuboring va ko'rsatmalarga amal qiling. Berilgan token — `BOT_TOKEN`.
2. [@userinfobot](https://t.me/userinfobot) ga `/start` yuboring. Ko'rsatilgan raqamli `Id` — `OWNER_ID`.

## Render'ga joylash

1. Loyihani GitHub'ga yuklang.
2. Render'da **New > Web Service** (yoki **Blueprint** — `render.yaml` bilan) tanlang va repozitoriyni ulang.
3. Runtime: **Docker**. Health check path: `/health`.
4. Environment o'zgaruvchilari: `BOT_TOKEN`, `OWNER_ID`, `WEBHOOK_SECRET` (Blueprint'da avtomatik yaratiladi; qo'lda ixtiyoriy uzun matn kiriting).
5. **Deploy** tugmasini bosing.
6. `https://<xizmat-nomi>.onrender.com/health` manzilini oching — `ok` ko'rinishi kerak.
7. Webhook'ni tekshiring: `https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo` — `url` to'ldirilgan va `last_error_message` bo'sh bo'lishi kerak.

`RENDER_EXTERNAL_URL` Render tomonidan avtomatik beriladi, bot webhook'ni o'zi o'rnatadi.

## Free va Starter ($7/oy)

| | Free | Starter |
|---|---|---|
| Uyqu | 15 daqiqa harakatsizlikdan keyin uxlaydi | Doim ishlaydi |
| Tanaffusdan keyingi birinchi xabar | ~1 daqiqa kechikib javob beradi | Darhol |
| Sessiya | Qayta ishga tushganda yo'qoladi | Faqat deploy/restartda yo'qoladi |

Ixtiyoriy: tashqi uptime monitor (masalan UptimeRobot) bilan `/health` manzilini har 5 daqiqada chaqirib, bepul nusxani uyg'oq saqlash mumkin.

## Cheklovlar

- Bulutdagi Bot API yuklab olish limiti — **20 MB**, yuborish limiti — **50 MB**.
- Instans qayta ishga tushganda sessiyalar yo'qoladi — faylni qayta yuboring.
- Zaif CPU tufayli uzun fayllar sekin ishlanadi (bir vaqtda bitta ishlov).
- WAV formatida teglar qo'llab-quvvatlanmaydi; OGG/Opus'da muqova saqlanmaydi.

## Lokal ishga tushirish

```bash
pip install -r requirements.txt
cp .env.example .env     # BOT_TOKEN va OWNER_ID ni to'ldiring
python main.py           # webhook o'zgaruvchilarisiz — polling rejimi
```

FFmpeg va ffprobe tizimda o'rnatilgan bo'lishi kerak. Sinov: `python -m scripts.selftest`.
