"""Incoming audio/video intake."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import texts
from handlers.common import is_busy, send_menu
from handlers.merge import add_file
from services import intake
from services import session as store
from services.tags import Tags
from utils import UserError

log = logging.getLogger(__name__)

router = Router(name="files")


@router.message(F.audio | F.voice | F.video | F.video_note | F.document)
async def on_file(message: Message, bot: Bot, state: FSMContext) -> None:
    """Start a new session from the sent file (or queue it in merge mode)."""
    uid = message.from_user.id
    inc = intake.extract_incoming(message)
    if inc is None:
        await message.answer(texts.UNSUPPORTED)
        return
    s = store.get(uid)
    if s is not None and s.mode == "merge":
        await add_file(bot, message, s, inc, state)
        return
    if is_busy() or (s is not None and s.running is not None):
        await message.answer(texts.BUSY)
        return
    try:
        intake.precheck_size(inc)
    except UserError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    store.end_session(uid)
    sid, work = store.allocate(uid)
    dest = work / f"v0.{inc.ext}"
    status = await message.answer(texts.DOWNLOADING)
    try:
        info = await intake.fetch(bot, inc, dest)
        cover = await intake.load_cover(work, dest, info)
    except UserError as exc:
        store.remove_dir(work)
        await status.edit_text(str(exc))
        return
    except Exception:
        store.remove_dir(work)
        raise
    s = store.register(uid, sid, work, dest, info, inc.stem)
    s.tags = Tags(info.title, info.artist, info.album)
    s.cover = cover
    log.info("new session %s: %s %.1fs %d bytes", sid, info.codec_name, info.duration, info.size)
    await status.delete()
    await send_menu(bot, message.chat.id, s)
