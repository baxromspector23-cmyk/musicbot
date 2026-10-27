"""Entry point: webhook mode on Render, long polling locally."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
from handlers import (
    common,
    convert,
    cut,
    effects,
    extract,
    files,
    instagram,
    menu,
    merge,
    ringtone,
    start,
    tags,
)
from middlewares import OwnerOnlyMiddleware
from services import db, ffmpeg, scheduler
from services import session as store

log = logging.getLogger("main")

_cleanup_task: asyncio.Task[None] | None = None
_keepalive_task: asyncio.Task[None] | None = None


async def keep_awake_loop() -> None:
    """Render Free tier spins down web services after 15 minutes of inactivity.

    This loop pings the service's own public /health endpoint every 8 minutes (480s).
    Render edge router treats this as incoming HTTP traffic, resetting the 15-minute
    sleep timer and ensuring continuous uptime.
    """
    import os
    import aiohttp

    target_base = (
        os.getenv("KEEP_AWAKE_URL", "").strip()
        or config.RENDER_EXTERNAL_URL
        or config.BASE_URL
        or ""
    ).rstrip("/")

    if not target_base:
        log.info("Keep-awake: no external URL configured (running in polling mode)")
        return

    health_url = f"{target_base}/health"
    tick_url = f"{target_base}/tick?secret={config.TICK_SECRET}"
    log.info("Keep-awake active: pinging %s every 5 minutes to prevent Render 15-min spin-down", health_url)

    # Initial delay so web server can bind port and start handling requests
    await asyncio.sleep(30)

    headers = {"User-Agent": "Mozilla/5.0 (compatible; UptimeMonitor/2.0; +https://render.com)"}
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(health_url) as resp:
                    if resp.status == 200:
                        log.debug("Keep-awake ping ok: %s -> %d", health_url, resp.status)
                    else:
                        log.warning("Keep-awake ping: %s returned status %d", health_url, resp.status)
                async with session.get(tick_url) as _:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.warning("Keep-awake ping error: %s (will retry in 5 minutes)", exc)

        await asyncio.sleep(300)  # 5 minutes (Render free sleeps after 15 min idle)


def setup_logging() -> None:
    """Log to stdout only."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def build_dispatcher() -> Dispatcher:
    """Create the dispatcher with middleware and routers."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(OwnerOnlyMiddleware(config.OWNER_ID))
    for r in (
        start.router,
        common.router,
        menu.router,
        tags.router,
        merge.router,
        cut.router,
        effects.router,
        convert.router,
        extract.router,
        ringtone.router,
        instagram.router,
        files.router,
        common.fallback_router,
    ):
        dp.include_router(r)
    dp.errors.register(common.on_error)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    return dp


async def on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    """Verify binaries, wipe temp, init db, start scheduler, set commands/webhook."""
    global _cleanup_task, _keepalive_task
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            log.critical("%s not found in PATH", tool)
            raise SystemExit(1)
    log.info(await ffmpeg.ffmpeg_version())
    log.info(await ffmpeg.ffprobe_version())
    store.wipe_root()
    await ffmpeg.get_caps()

    # Initialize persistent SQLite/Postgres database
    await db.init_db()

    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="new", description="Yangi fayl"),
        BotCommand(command="reel", description="Instagram Reel rejalashtirish"),
        BotCommand(command="queue", description="Rejalashtirilgan postlar"),
        BotCommand(command="history", description="Postlar tarixi"),
        BotCommand(command="stats", description="24 soatlik statistika"),
        BotCommand(command="igstatus", description="Instagram ulanish holati"),
        BotCommand(command="rek", description="AI orqali Instagram profilni Rek uchun moslash"),
        BotCommand(command="iglogin", description="Instagram'ga ulanish (Facebook'siz)"),
        BotCommand(command="igtoken", description="Instagram tokenni yangilash"),
        BotCommand(command="cancel", description="Bekor qilish"),
    ])

    if config.BASE_URL:
        await bot.set_webhook(
            config.BASE_URL + config.WEBHOOK_PATH,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        log.info("webhook set to %s%s", config.BASE_URL, config.WEBHOOK_PATH)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("no public URL configured: polling mode")

    _cleanup_task = asyncio.create_task(store.cleanup_loop())
    _keepalive_task = asyncio.create_task(keep_awake_loop())
    scheduler.start_scheduler(bot)


async def on_shutdown() -> None:
    """Stop background tasks (the webhook is intentionally kept)."""
    scheduler.stop_scheduler()
    if _cleanup_task is not None:
        _cleanup_task.cancel()
    if _keepalive_task is not None:
        _keepalive_task.cancel()


async def health(_: web.Request) -> web.Response:
    """Health check for Render."""
    age = scheduler.get_heartbeat_age_sec()
    if age > 120 and scheduler._LAST_TICK_TS > 0:
        log.warning("Scheduler heartbeat stale: %.1fs ago", age)
    return web.Response(text="ok")


async def tick(request: web.Request) -> web.Response:
    """External tick endpoint (e.g. cron-job.org / wakeup for Render free sleeping)."""
    secret = request.query.get("secret") or request.headers.get("X-Tick-Secret")
    json_payload = None
    if request.can_read_body:
        try:
            if "application/json" in (request.content_type or ""):
                json_payload = await request.json()
                if not secret and isinstance(json_payload, dict):
                    secret = json_payload.get("secret")
            else:
                data = await request.post()
                if not secret:
                    secret = data.get("secret")
        except Exception:
            pass

    if not config.TICK_SECRET or secret != config.TICK_SECRET:
        return web.Response(status=401, text="unauthorized")

    if isinstance(json_payload, dict):
        if "sync_ig_session" in json_payload and isinstance(json_payload["sync_ig_session"], dict):
            await db.set_kv("ig_direct_session", json_payload["sync_ig_session"])
        if "sync_post" in json_payload and isinstance(json_payload["sync_post"], dict):
            from datetime import datetime
            sp = json_payload["sync_post"]
            sched_dt = datetime.fromisoformat(sp["scheduled_at"]) if sp.get("scheduled_at") else None
            await db.create_post(
                owner_id=int(sp.get("owner_id") or config.OWNER_ID),
                tg_file_id=sp.get("tg_file_id"),
                file_unique_id=sp.get("file_unique_id"),
                duration=float(sp.get("duration") or 24.0),
                width=int(sp.get("width") or 1080),
                height=int(sp.get("height") or 1920),
                caption=sp.get("caption"),
                hashtags=sp.get("hashtags") or [],
                scheduled_at=sched_dt,
                status=sp.get("status") or "scheduled",
            )

    bot: Bot = request.app["bot"]
    published_count = await scheduler.process_due_posts(bot)
    await scheduler.check_daily_token_refresh(bot)
    scheduled_list = await db.list_scheduled_posts(limit=50)
    return web.json_response({
        "ok": True,
        "published": published_count,
        "scheduled_count": len(scheduled_list),
    })


async def serve_media(request: web.Request) -> web.Response:
    """Serve temporary prepared Reel video to Meta crawler protected by a random token, then delete."""
    token = request.match_info.get("token") or request.query.get("token", "")
    filename = request.match_info.get("filename", "")
    if (
        not token
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or not filename.endswith(".mp4")
    ):
        return web.Response(status=403, text="forbidden")

    file_path = scheduler.get_media_file(token, filename)
    if file_path is None or not file_path.exists():
        return web.Response(status=404, text="not found")

    if request.method == "HEAD":
        return web.Response(
            status=200,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(file_path.stat().st_size),
            },
        )

    data = file_path.read_bytes()
    # Delete file and revoke token once Meta fetches the full video
    scheduler.consume_media_token(token)
    return web.Response(
        body=data,
        content_type="video/mp4",
        headers={"Content-Length": str(len(data))},
    )


def create_app(bot: Bot, dp: Dispatcher) -> web.Application:
    """aiohttp app with /health, /tick, /media and the Telegram webhook route."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", health)
    app.router.add_get("/tick", tick)
    app.router.add_post("/tick", tick)
    app.router.add_get("/media/{token}/{filename}", serve_media)
    app.router.add_get("/media/{filename}", serve_media)

    # handle_in_background=True (aiogram default, verified): the update is processed in a
    # background task and the HTTP response is returned immediately, so FFmpeg never blocks it.
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, handle_in_background=True, secret_token=config.WEBHOOK_SECRET,
    ).register(app, path=config.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def close_bot(_: web.Application) -> None:
        await bot.session.close()

    app.on_cleanup.append(close_bot)
    return app


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    """Local fallback: long polling."""
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    """Start the bot."""
    setup_logging()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    if config.BASE_URL:
        web.run_app(create_app(bot, dp), host="0.0.0.0", port=config.PORT, print=None)
    else:
        asyncio.run(run_polling(bot, dp))


if __name__ == "__main__":
    main()
