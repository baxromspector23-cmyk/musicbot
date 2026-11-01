"""Cloud Autopilot runner for GitHub Actions (runs 4x/day in the cloud even when PC is off)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

import config
from services import autopilot, db, ig_direct

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cloud_autopilot")

_TZ = ZoneInfo("Asia/Tashkent")
# 24-hour marathon: 1 Reel every hour
MARATHON_START = datetime(2026, 10, 30, 0, 0, tzinfo=_TZ)
MARATHON_END = datetime(2026, 10, 31, 0, 0, tzinfo=_TZ)
# Regular posting hours outside the marathon (Tashkent: 11:00, 15:00, 20:00)
REGULAR_HOURS = {11, 15, 20}


async def send_marathon_report(st: dict) -> None:
    """Collect real stats of every Reel posted during the marathon and send a rich Telegram report."""
    settings = await ig_direct.load_direct_settings()

    def _collect() -> list:
        cl = ig_direct._create_client(settings)
        return cl.user_medias_v1(cl.user_id, amount=60)

    medias = await asyncio.to_thread(_collect)
    rows = []
    for m in medias:
        t = m.taken_at
        if t is None:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t = t.astimezone(_TZ)
        if MARATHON_START <= t < MARATHON_END:
            rows.append({
                "time": t, "code": m.code,
                "plays": int(getattr(m, "play_count", 0) or getattr(m, "view_count", 0) or 0),
                "likes": int(m.like_count or 0), "cmts": int(m.comment_count or 0),
            })
    rows.sort(key=lambda r: r["time"])
    tp = sum(r["plays"] for r in rows)
    tl = sum(r["likes"] for r in rows)
    tc = sum(r["cmts"] for r in rows)
    top = sorted(rows, key=lambda r: r["plays"], reverse=True)[:5]

    hourly = {}
    for r in rows:
        hourly.setdefault(r["time"].hour, 0)
        hourly[r["time"].hour] += r["plays"]
    best_hours = sorted(hourly.items(), key=lambda x: x[1], reverse=True)[:3]

    lines = [
        "📊 <b>24 SOATLIK MARAFON YAKUNLANDI!</b>",
        "<i>30-oktabr 00:00 → 31-oktabr 00:00 (Toshkent)</i>",
        "",
        f"👤 <b>@{st['username']}</b> — obunachilar: <b>{st['followers']}</b>",
        "",
        f"🎬 Joylangan videolar: <b>{len(rows)}/24</b>",
        f"👁 Jami ko'rishlar: <b>{tp:,}</b>",
        f"❤️ Jami layklar: <b>{tl:,}</b>",
        f"💬 Jami kommentlar: <b>{tc:,}</b>",
        f"📈 O'rtacha (1 video): <b>{(tp // max(len(rows), 1)):,}</b> ko'rish",
        "",
        "🏆 <b>TOP-5 video:</b>",
    ]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, r in enumerate(top):
        lines.append(
            f"{medals[i]} <a href='https://www.instagram.com/reel/{r['code']}/'>{r['time']:%H:%M}</a> — "
            f"👁 {r['plays']:,} | ❤️ {r['likes']} | 💬 {r['cmts']}"
        )
    if best_hours:
        lines += ["", "⏰ <b>Eng yaxshi soatlar:</b> " + ", ".join(f"{h:02d}:00 ({p:,})" for h, p in best_hours)]
    lines += ["", "🕐 <b>Soatma-soat:</b>"]
    for r in rows:
        lines.append(f"<code>{r['time']:%H:%M}</code> 👁{r['plays']} ❤️{r['likes']} 💬{r['cmts']}")

    text = "\n".join(lines)
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        for i in range(0, len(text), 3900):
            await bot.send_message(config.OWNER_ID, text[i:i + 3900], disable_web_page_preview=True)
    finally:
        await bot.session.close()
    log.info("Marathon report sent (%d reels)", len(rows))


async def restore_session_from_env() -> None:
    """Restore ig_direct_session from IG_DIRECT_SESSION_B64 secret in cloud environments and sync to Render."""
    b64_raw = os.getenv("IG_DIRECT_SESSION_B64", "").strip()
    if not b64_raw:
        return
    try:
        decoded = base64.b64decode(b64_raw).decode("utf-8")
        sess_obj = json.loads(decoded)
        if isinstance(sess_obj, dict):
            if "encrypted_settings" in sess_obj:
                await db.set_kv("ig_direct_session", sess_obj)
            else:
                await ig_direct.save_direct_session(
                    sess_obj,
                    username=str(sess_obj.get("_username") or "test_aibot"),
                    user_id=str(sess_obj.get("_user_id") or "15317682957"),
                )
            log.info("Restored ig_direct_session from IG_DIRECT_SESSION_B64 environment secret")
            # Keep Render server synced with active session
            try:
                import urllib.request
                kv_rec = await db.get_kv("ig_direct_session")
                if kv_rec and config.TICK_SECRET:
                    payload = json.dumps({"secret": config.TICK_SECRET, "sync_ig_session": kv_rec}).encode("utf-8")
                    req = urllib.request.Request(
                        "https://musicbot-yy58.onrender.com/tick",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=15).read()
                    log.info("Synced ig_direct_session to Render server")
            except Exception as sync_exc:
                log.debug("Render session sync warning: %s", sync_exc)
    except Exception as exc:
        log.warning("Failed to decode IG_DIRECT_SESSION_B64: %s", exc)


def pick_slot_index_for_now() -> int:
    """Choose slot index based on current Asia/Tashkent hour (11:00 -> 0, 15:00 -> 1, 20:00 -> 2)."""
    tz = ZoneInfo(config.TZ)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    local_hour = now_local.hour

    if local_hour < 13:
        return 0
    if local_hour < 18:
        return 1
    return 2


async def main() -> None:
    check_only = "--check-only" in sys.argv
    force_post = "--force" in sys.argv

    await db.init_db()
    await restore_session_from_env()

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(_TZ)
    if not force_post and not check_only:
        if not (MARATHON_START <= now_local < MARATHON_END) and now_local.hour not in REGULAR_HOURS:
            log.info("Not a posting hour (%02d:00); skipping without hitting Instagram API.", now_local.hour)
            return

    st = await ig_direct.get_live_profile_stats(limit=10)
    log.info(
        "Connected to Instagram @%s (followers=%d, medias=%d)",
        st["username"], st["followers"], st["media_count"],
    )

    if check_only:
        log.info("Cloud check-only mode succeeded!")
        return

    # Guard against duplicate posting in the same hour
    _now_l = now_local
    is_report_time = MARATHON_END <= _now_l < MARATHON_END + timedelta(minutes=59)
    if not force_post and not is_report_time and st["items"]:
        latest = st["items"][0]
        taken_at = latest.get("taken_at")
        if taken_at is not None:
            if taken_at.tzinfo is None:
                taken_at = taken_at.replace(tzinfo=timezone.utc)
            taken_local = taken_at.astimezone(_TZ)
            age_minutes = (now_utc - taken_at).total_seconds() / 60.0
            log.info("Latest Instagram Reel (%s) was posted %.1f minutes ago (%s)", latest["code"], age_minutes, taken_local.strftime("%H:%M"))
            if (taken_local.date() == _now_l.date() and taken_local.hour == _now_l.hour) or age_minutes < 45:
                log.info("Already posted in hour %02d:00 (%.1f min ago); skipping duplicate.", _now_l.hour, age_minutes)
                return

    slot_idx = pick_slot_index_for_now()

    if MARATHON_START <= now_local < MARATHON_END:
        slot_idx = now_local.hour % len(autopilot.DAILY_SLOTS_SPEC)
        log.info("24h marathon: hourly post #%d (slot %d)", now_local.hour + 1, slot_idx)

    for i, arg in enumerate(sys.argv):
        if arg == "--slot" and i + 1 < len(sys.argv):
            try:
                slot_idx = int(sys.argv[i + 1])
            except Exception:
                pass
    spec = autopilot.DAILY_SLOTS_SPEC[slot_idx % len(autopilot.DAILY_SLOTS_SPEC)]
    log.info("Running Cloud Autopilot for slot %d: %s (%s)", slot_idx, spec["label"], spec["niche"])

    vid_info = await autopilot.find_and_download_b2b_video(slot_idx)
    norm_path = vid_info["path"]
    try:
        caption_text = vid_info.get("caption") or spec.get("caption", "")
        full_caption = f"{caption_text}\n\n{' '.join('#' + t for t in spec['hashtags'])}"
        media_id, permalink = await ig_direct.upload_reel(norm_path, full_caption)
        log.info("Successfully published Cloud Autopilot Reel: %s", permalink)

        # Ensure code is persisted
        await autopilot._mark_code_used(vid_info["code"])

        bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        try:
            await bot.send_message(
                config.OWNER_ID,
                f"☁️🤖 <b>Bulutli Avtopilot ({spec['label']}): Yangi Reel joylandi!</b>\n\n"
                f"🎯 <b>Yo'nalish:</b> {spec['niche']}\n"
                f"🔗 <b>Ko'rish:</b> <a href='{permalink}'>{permalink}</a>",
            )
        finally:
            await bot.session.close()
    finally:
        norm_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
