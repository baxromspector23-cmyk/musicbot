"""Entry point: webhook mode on Render, long polling locally."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
from handlers import common, convert, cut, effects, extract, files, menu, merge, ringtone, start, tags
from middlewares import OwnerOnlyMiddleware
from services import ffmpeg
from services import session as store

log = logging.getLogger("main")

_cleanup_task: asyncio.Task[None] | None = None


def setup_logging() -> None:
    """Log to stdout only."""
    logging.basicConfig(
        stream=sys.stdout, level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def build_dispatcher() -> Dispatcher:
    """Create the dispatcher with middleware and routers."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(OwnerOnlyMiddleware(config.OWNER_ID))
    for r in (start.router, common.router, menu.router, tags.router, merge.router, cut.router,
              effects.router, convert.router, extract.router, ringtone.router, files.router,
              common.fallback_router):
        dp.include_router(r)
    dp.errors.register(common.on_error)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    return dp


async def on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    """Verify binaries, wipe temp, detect capabilities, set commands/webhook."""
    global _cleanup_task
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            log.critical("%s not found in PATH", tool)
            raise SystemExit(1)
    log.info(await ffmpeg.ffmpeg_version())
    log.info(await ffmpeg.ffprobe_version())
    store.wipe_root()
    await ffmpeg.get_caps()
    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="new", description="Yangi fayl"),
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


async def on_shutdown() -> None:
    """Stop background tasks (the webhook is intentionally kept)."""
    if _cleanup_task is not None:
        _cleanup_task.cancel()


async def health(_: web.Request) -> web.Response:
    """Health check for Render."""
    return web.Response(text="ok")


def create_app(bot: Bot, dp: Dispatcher) -> web.Application:
    """aiohttp app with /health and the Telegram webhook route."""
    app = web.Application()
    app.router.add_get("/health", health)
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
    bot = Bot(config.BOT_TOKEN)
    dp = build_dispatcher()
    if config.BASE_URL:
        web.run_app(create_app(bot, dp), host="0.0.0.0", port=config.PORT, print=None)
    else:
        asyncio.run(run_polling(bot, dp))


if __name__ == "__main__":
    main()
