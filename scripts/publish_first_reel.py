import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("publish_first_reel")

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from services import autopilot, db, ig_direct
from utils import escape_html


async def main():
    log.info("Starting initial launch Reel publication for @test_aibot...")
    await db.init_db()

    # 1. Download & verify top video for slot 0 (Top AI Saytlar & Hayotiy Xaklar)
    spec = autopilot.DAILY_SLOTS_SPEC[0]
    log.info("Searching & preparing viral AI Reel for slot: %s (%s)...", spec["label"], spec["niche"])
    vid_info = await autopilot.find_and_download_b2b_video(0)
    video_path: Path = vid_info["path"]
    log.info("Video prepared: code=%s duration=%.1fs path=%s size=%d bytes",
             vid_info["code"], vid_info["duration"], video_path, video_path.stat().st_size)

    # 2. Build caption
    full_caption = f"{spec['caption']}\n\n{' '.join('#' + t for t in spec['hashtags'])}"
    log.info("Uploading Reel to Instagram (@test_aibot)...")

    # 3. Direct Instagram upload
    media_id, permalink = await ig_direct.upload_reel(video_path, full_caption)
    log.info("Uploaded successfully! Media ID=%s Permalink=%s", media_id, permalink)

    # 4. Save to database as published post
    now_utc = datetime.now(timezone.utc)
    post = await db.create_post(
        owner_id=config.OWNER_ID,
        duration=vid_info["duration"],
        width=1080,
        height=1920,
        caption=full_caption,
        hashtags=spec["hashtags"],
        scheduled_at=now_utc,
        status="published",
    )
    await db.update_post(
        post.id,
        ig_media_id=media_id,
        permalink=permalink,
        published_at=now_utc,
    )
    log.info("Post saved in DB: post_id=%d", post.id)

    # 5. Clean up downloaded video
    video_path.unlink(missing_ok=True)

    # 6. Notify owner in Telegram
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        msg = (
            f"🚀 <b>Chellenj boshlandi! 1-Reel muvaffaqiyatli joylandi!</b>\n\n"
            f"👤 Akkaunt: <b>@test_aibot</b>\n"
            f"🎬 Mavzu: <b>{escape_html(spec['niche'])}</b>\n"
            f"⏱ Davomiyligi: <code>{vid_info['duration']}s</code> (9:16 vertikal)\n"
            f"🏷 Suv belgisi: <b>@test_aibot</b>\n"
            f"🔗 <b>Reels havolasi:</b> <a href=\"{permalink}\">{permalink}</a>\n\n"
            f"⚡️ <i>Bugun keyingi avtopilot vaqti: 20:00 da!</i>"
        )
        await bot.send_message(config.OWNER_ID, msg, disable_web_page_preview=False)
        log.info("Telegram notification sent to owner %d", config.OWNER_ID)
    finally:
        await bot.session.close()

    print(f"DONE_PERMALINK: {permalink}")


if __name__ == "__main__":
    asyncio.run(main())
