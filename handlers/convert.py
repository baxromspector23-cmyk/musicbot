"""Convert handler (buttons only)."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import keyboards as kb
import texts
from handlers.common import execute, require_session
from keyboards import MenuCb, OpCb
from services import ffmpeg
from services import session as store

router = Router(name="convert")

LOSSY = ("mp3", "m4a", "ogg")


@router.callback_query(MenuCb.filter(F.action == "conv"))
async def conv_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.clear()
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.CONVERT_CHOOSE, reply_markup=kb.convert_formats(s.sid))


async def _run(bot: Bot, chat_id: int, sid: str, fmt: str, bitrate: int | None) -> None:
    s = store.get(chat_id)
    if s is None or s.sid != sid:
        return
    src, info = s.current, s.info
    note = ""
    if bitrate and info.bit_rate and bitrate * 1000 > info.bit_rate * 1.05:
        note = texts.QUALITY_NOTE

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.convert(src, dst, info, bitrate, job)

    label = texts.STEP_CONVERT.format(fmt=f"{fmt}{f' {bitrate}k' if bitrate else ''}")
    await execute(bot, chat_id, s, label, fmt, run, note=note)


@router.callback_query(OpCb.filter(F.action == "conv_fmt"))
async def conv_fmt(cb: CallbackQuery, callback_data: OpCb, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    fmt = callback_data.arg
    if fmt in LOSSY:
        await bot.send_message(cb.from_user.id, texts.CONVERT_BITRATE, reply_markup=kb.convert_bitrates(s.sid, fmt))
    else:
        await _run(bot, cb.from_user.id, s.sid, fmt, None)


@router.callback_query(OpCb.filter(F.action == "conv_br"))
async def conv_br(cb: CallbackQuery, callback_data: OpCb, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    fmt, _, br = callback_data.arg.partition(":")
    await _run(bot, cb.from_user.id, s.sid, fmt, int(br))
