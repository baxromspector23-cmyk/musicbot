"""Cloud Autopilot runner for GitHub Actions (runs 4x/day in the cloud even when PC is off)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

import config
from services import autopilot, db, ig_direct

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cloud_autopilot")


async def restore_session_from_env() -> None:
    """Restore ig_direct_session from IG_DIRECT_SESSION_B64 secret in cloud environments."""
    b64_raw = os.getenv("IG_DIRECT_SESSION_B64", "").strip()
    if not b64_raw:
        return
    try:
        decoded = base64.b64decode(b64_raw).decode("utf-8")
        sess_obj = json.loads(decoded)
        if isinstance(sess_obj, dict):
            await db.set_kv("ig_direct_session", sess_obj)
            log.info("Restored ig_direct_session from IG_DIRECT_SESSION_B64 environment secret")
    except Exception as exc:
        log.warning("Failed to decode IG_DIRECT_SESSION_B64: %s", exc)


def pick_slot_index_for_now() -> int:
    """Choose slot index based on current Asia/Tashkent hour/minute."""
    tz = ZoneInfo(config.TZ)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    local_hour = now_local.hour
    local_min = now_local.minute

    # 09:00 -> Slot 0 (Restoran va Kafe)
    if local_hour < 10:
        return 0
    # 11:00 -> Slot 1 (Kompaniya va Veb-Sayt)
    if local_hour == 10 or (local_hour == 11 and local_min < 30):
        return 1
    # 12:00 -> Slot 2 (Zamonaviy UI/UX va Interaktiv Saytlar)
    if (local_hour == 11 and local_min >= 30) or local_hour in (12, 13, 14):
        return 2
    # 18:30 -> Slot 3 (Do'konlar uchun Internet-Do'kon)
    if local_hour < 19:
        return 3
    # 20:30 -> Slot 4 (Telegram Bot va Avtomatlashtirish)
    return 4


async def main() -> None:
    check_only = "--check-only" in sys.argv
    force_post = "--force" in sys.argv

    await db.init_db()
    await restore_session_from_env()

    st = await ig_direct.get_live_profile_stats(limit=10)
    log.info(
        "Connected to Instagram @%s (followers=%d, medias=%d)",
        st["username"], st["followers"], st["media_count"],
    )

    if check_only:
        log.info("Cloud check-only mode succeeded!")
        return

    # Guard against duplicate posting if another worker (PC or Render) already posted within last 80 minutes
    now_utc = datetime.now(timezone.utc)
    if not force_post and st["items"]:
        latest = st["items"][0]
        taken_at = latest.get("taken_at")
        if taken_at is not None:
            if taken_at.tzinfo is None:
                taken_at = taken_at.replace(tzinfo=timezone.utc)
            age_minutes = (now_utc - taken_at).total_seconds() / 60.0
            log.info("Latest Instagram Reel (%s) was posted %.1f minutes ago", latest["code"], age_minutes)
            if age_minutes < 35:
                log.info("A Reel was already posted %.1f min ago (<35 min); skipping duplicate cloud post.", age_minutes)
                return

    slot_idx = pick_slot_index_for_now()
    spec = autopilot.DAILY_SLOTS_SPEC[slot_idx]
    log.info("Running Cloud Autopilot for slot %d: %s (%s)", slot_idx, spec["label"], spec["niche"])

    vid_info = await autopilot.find_and_download_b2b_video(slot_idx)
    norm_path = vid_info["path"]
    try:
        full_caption = f"{spec['caption']}\n\n{' '.join('#' + t for t in spec['hashtags'])}"
        media_id, permalink = await ig_direct.upload_reel(norm_path, full_caption)
        log.info("Successfully published Cloud Autopilot Reel: %s", permalink)

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
