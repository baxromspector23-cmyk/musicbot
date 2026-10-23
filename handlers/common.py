"""Shared handler helpers, job execution, cancel/undo/done and error handling."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

import config
import keyboards as kb
import texts
from keyboards import MenuCb, OpCb
from services import session as store
from services.ffmpeg import FFmpegCancelled, FFmpegError, FFmpegTimeout, InvalidInput, Job, probe
from services.sender import send_result
from services.session import JOB_SEM, Session
from services.tags import Tags, tags_supported, write_tags
from utils import escape_html, fmt_duration, progress_bar

log = logging.getLogger(__name__)

router = Router(name="common")
fallback_router = Router(name="fallback")

KEEP: object = object()
Runner = Callable[[Path, Job], Awaitable[None]]


# ----------------------------------------------------------------- helpers

async def require_session(cb: CallbackQuery, sid: str) -> Session | None:
    """Return the matching session or answer with an expired alert."""
    s = store.get_valid(cb.from_user.id, sid)
    if s is None:
        await cb.answer(texts.SESSION_EXPIRED, show_alert=True)
    return s


async def require_session_msg(message: Message, state: FSMContext) -> Session | None:
    """Session for a text message in an FSM state; clears state if missing."""
    s = store.get(message.from_user.id) if message.from_user else None
    if s is None:
        await state.clear()
        await message.answer(texts.SESSION_EXPIRED)
    return s


def is_busy() -> bool:
    """True while an FFmpeg job runs."""
    return JOB_SEM.locked()


def format_tags(t: Tags) -> str:
    """One-line tags description."""
    if t.is_empty:
        return texts.CARD_NO_TAGS
    title = escape_html(t.title or "—")
    artist = escape_html(t.artist or "—")
    album = f" ({escape_html(t.album)})" if t.album else ""
    return texts.TAG_LINE.format(title=title, artist=artist) + album


def info_card(s: Session) -> str:
    """Info card for the current file."""
    i = s.info
    br = f" {round(i.bit_rate / 1000)} kbps" if i.bit_rate else ""
    name = escape_html(s.source_name)
    text = texts.CARD.format(
        name=name, duration=fmt_duration(i.duration),
        fmt=f"{i.codec_name or i.ext}{br}", tags=format_tags(s.tags),
    )
    if s.steps:
        text += texts.CARD_STEPS.format(steps=" → ".join(escape_html(step) for step in s.steps))
    return text


async def send_menu(bot: Bot, chat_id: int, s: Session, prefix: str = "") -> None:
    """Send the info card with the main menu."""
    text = f"{prefix}\n\n{info_card(s)}" if prefix else info_card(s)
    await bot.send_message(chat_id, text, reply_markup=kb.main_menu(s.sid, s.info.has_video))


async def safe_edit(msg: Message, text: str, markup=None) -> None:  # type: ignore[no-untyped-def]
    """Edit a message ignoring 'not modified'/deleted errors."""
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        log.debug("edit skipped: %s", exc)


async def execute(
    bot: Bot,
    chat_id: int,
    s: Session,
    label: str,
    ext: str,
    runner: Runner,
    *,
    new_tags: Tags | None = None,
    new_cover: object = KEEP,
    note: str = "",
) -> bool:
    """Run one operation under the global semaphore, commit the result and send it."""
    if JOB_SEM.locked() or s.running is not None:
        await bot.send_message(chat_id, texts.BUSY)
        return False
    async with JOB_SEM:
        status = await bot.send_message(
            chat_id, f"{texts.WORKING}\n{progress_bar(0)}", reply_markup=kb.job_cancel(s.sid))

        async def on_progress(pct: float) -> None:
            await safe_edit(status, f"{texts.WORKING}\n{progress_bar(pct)}", kb.job_cancel(s.sid))

        job = Job(on_progress=on_progress)
        s.running = job
        dst = store.next_path(s, ext)
        committed = False
        started = time.monotonic()
        in_size = s.current.stat().st_size if s.current.exists() else 0
        failure = ""
        try:
            await runner(dst, job)
            info = await probe(dst)
            tags = new_tags if new_tags is not None else s.tags
            cover = s.cover if new_cover is KEEP else new_cover
            if tags_supported(dst.suffix) and (new_cover is not KEEP or new_tags is not None or not tags.is_empty or cover):
                try:
                    await asyncio.to_thread(write_tags, dst, tags, cover)  # type: ignore[arg-type]
                except Exception:
                    log.warning("could not write tags", exc_info=True)
            if not store.is_registered(s):
                return False
            store.commit(s, dst, info, label, tags, cover)  # type: ignore[arg-type]
            committed = True
            log.info("op=%s in=%d out=%d took=%.1fs", label, in_size, dst.stat().st_size,
                     time.monotonic() - started)
        except FFmpegCancelled:
            failure = texts.JOB_CANCELLED
        except FFmpegTimeout:
            failure = texts.FFMPEG_TIMEOUT
        except InvalidInput as exc:
            failure = escape_html(str(exc))
        except FFmpegError:
            failure = texts.FFMPEG_FAIL
        finally:
            s.running = None
            if not committed:
                dst.unlink(missing_ok=True)
        if not committed:
            await safe_edit(status, failure or texts.FFMPEG_FAIL)
            if store.is_registered(s):
                await send_menu(bot, chat_id, s)
            return False
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        await send_result(bot, chat_id, s, s.current, s.info)
        text = texts.RESULT_PROMPT + (f"\n{note}" if note else "")
        await bot.send_message(chat_id, text, reply_markup=kb.after_result(s.sid))
        return True


# ----------------------------------------------------------------- handlers

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot) -> None:
    """/cancel: stop job, leave state/merge and show the menu."""
    await state.clear()
    s = store.get(message.from_user.id)
    if s is None:
        await message.answer(texts.CANCELLED)
        return
    if s.running is not None:
        s.running.cancel()
        return
    if s.mode == "merge":
        store.clear_merge(s)
    await send_menu(bot, message.chat.id, s, texts.CANCELLED)


@router.callback_query(OpCb.filter(F.action == "jobcancel"))
async def job_cancel(cb: CallbackQuery, callback_data: OpCb) -> None:
    """Cancel the running FFmpeg job."""
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if s.running is not None:
        s.running.cancel()
    await cb.answer()


@router.callback_query(MenuCb.filter(F.action == "undo"))
async def undo(cb: CallbackQuery, callback_data: MenuCb, bot: Bot, state: FSMContext) -> None:
    """Restore the previous version."""
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if s.running is not None or is_busy():
        await cb.answer(texts.BUSY, show_alert=True)
        return
    if not store.undo(s):
        await cb.answer(texts.NOTHING_TO_UNDO, show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await send_menu(bot, cb.from_user.id, s, texts.UNDONE)


@router.callback_query(MenuCb.filter(F.action == "done"))
async def done(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    """Finish the session and delete temp files."""
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.clear()
    store.end_session(cb.from_user.id)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.DONE)


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Global error handler: log traceback and tell the owner briefly."""
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "not modified" in str(exc).lower():
        return True
    log.error("unhandled error: %r", exc, exc_info=exc)
    if config.OWNER_ID > 0:
        try:
            await bot.send_message(config.OWNER_ID, texts.ERROR_GENERIC)
        except Exception:
            log.debug("could not notify owner", exc_info=True)
    return True


@fallback_router.message(StateFilter(None), F.text)
async def stray_text(message: Message, bot: Bot) -> None:
    """Text with no active state: hint to send a file or show the menu."""
    s = store.get(message.from_user.id)
    if s is None:
        await message.answer(texts.SEND_FILE_FIRST)
    else:
        await send_menu(bot, message.chat.id, s, texts.USE_MENU)
