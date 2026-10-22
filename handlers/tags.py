"""Tags and cover editing."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import texts
from handlers.common import KEEP, execute, is_busy, require_session, require_session_msg
from keyboards import MenuCb, OpCb
from services import ffmpeg
from services import session as store
from services.session import JOB_SEM, Session
from services.tags import Tags, cover_supported, tags_supported
from states import TagStates
from utils import clean_text

router = Router(name="tags")

FIELD_NAMES = {"title": texts.TAG_TITLE, "artist": texts.TAG_ARTIST, "album": texts.TAG_ALBUM}


async def _apply(bot: Bot, chat_id: int, s: Session, label: str, new_tags: Tags | None,
                 new_cover: object, note: str = "") -> None:
    """Copy current file to the next version and write the new tags into the copy."""
    src = s.current
    ext = src.suffix.lstrip(".")

    async def run(dst: Path, job: ffmpeg.Job) -> None:
        await asyncio.to_thread(shutil.copy2, src, dst)

    await execute(bot, chat_id, s, label, ext, run, new_tags=new_tags, new_cover=new_cover, note=note)


@router.callback_query(MenuCb.filter(F.action == "tags"))
async def tags_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if not tags_supported(s.current.suffix):
        await cb.answer(texts.TAGS_UNSUPPORTED, show_alert=True)
        return
    await state.set_state(TagStates.choosing_field)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.TAGS_CHOOSE, reply_markup=kb.tags_kb(s.sid))


@router.callback_query(OpCb.filter(F.action == "tag"))
async def tag_choice(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    arg = callback_data.arg
    chat_id = cb.from_user.id
    await cb.answer()
    if arg == "show":
        await bot.send_message(chat_id, texts.TAGS_SHOW.format(
            title=s.tags.title or "—", artist=s.tags.artist or "—", album=s.tags.album or "—",
            cover=texts.TAGS_YES if s.cover else texts.TAGS_NO), reply_markup=kb.tags_kb(s.sid))
    elif arg in FIELD_NAMES:
        await state.set_state(TagStates.waiting_text)
        await state.update_data(field=arg)
        await bot.send_message(chat_id, texts.TAGS_ASK.format(field=FIELD_NAMES[arg]),
                               reply_markup=kb.input_back(s.sid))
    elif arg == "cover":
        if not cover_supported(s.current.suffix):
            await bot.send_message(chat_id, texts.TAGS_OGG_NO_COVER, reply_markup=kb.tags_kb(s.sid))
            return
        await state.set_state(TagStates.waiting_cover)
        await bot.send_message(chat_id, texts.TAGS_ASK_COVER, reply_markup=kb.input_back(s.sid))
    elif arg == "delcover":
        await state.clear()
        await _apply(bot, chat_id, s, texts.STEP_TAGS.format(field=texts.TAG_COVER), None, None)


@router.message(TagStates.waiting_text, F.text)
async def tag_text(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    value = clean_text(message.text or "")
    if not value:
        await message.answer(texts.TAGS_EMPTY)
        return
    field = (await state.get_data()).get("field", "title")
    if field not in FIELD_NAMES:
        await state.clear()
        return
    tags = s.tags.copy()
    setattr(tags, field, value)
    await state.clear()
    await _apply(bot, message.chat.id, s, texts.STEP_TAGS.format(field=FIELD_NAMES[field]), tags, KEEP)


@router.message(TagStates.waiting_cover, F.photo | F.document)
async def tag_cover(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        doc = message.document
        ext = Path(doc.file_name or "").suffix.lower().lstrip(".")
        if not ((doc.mime_type or "").startswith("image/") or ext in config.IMAGE_EXT):
            await message.answer(texts.TAGS_BAD_COVER)
            return
        file_id = doc.file_id
    if is_busy():
        await message.answer(texts.BUSY)
        return
    raw = s.work_dir / "cover_in.img"
    out = s.work_dir / "cover_new.jpg"
    try:
        async with JOB_SEM:
            try:
                await bot.download(file_id, destination=raw)
            except TelegramBadRequest as exc:
                if "too big" in str(exc).lower():
                    await message.answer(texts.FILE_TOO_BIG)
                    return
                raise
            try:
                await ffmpeg.make_cover(raw, out)
            except ffmpeg.FFmpegError:
                await message.answer(texts.TAGS_BAD_COVER)
                return
            data = await asyncio.to_thread(out.read_bytes)
    finally:
        raw.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
    if not store.is_registered(s):
        return
    await state.clear()
    await _apply(bot, message.chat.id, s, texts.STEP_TAGS.format(field=texts.TAG_COVER), None, data)


@router.message(TagStates.waiting_cover)
async def tag_cover_bad(message: Message) -> None:
    await message.answer(texts.TAGS_BAD_COVER)
