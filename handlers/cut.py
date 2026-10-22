"""Cut handler."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import texts
from handlers.common import execute, is_busy, require_session, require_session_msg
from keyboards import MenuCb
from services import ffmpeg
from states import CutStates
from utils import fmt_duration, parse_range, validate_range

router = Router(name="cut")


@router.callback_query(MenuCb.filter(F.action == "cut"))
async def cut_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.set_state(CutStates.waiting_range)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.CUT_ASK.format(duration=fmt_duration(s.info.duration)),
                           reply_markup=kb.input_back(s.sid))


@router.message(CutStates.waiting_range, F.text)
async def cut_range(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    if is_busy():
        await message.answer(texts.BUSY)
        return
    try:
        start, end_raw = parse_range(message.text or "")
        start, end = validate_range(start, end_raw, s.info.duration)
    except ValueError:
        await message.answer(texts.BAD_TIME.format(duration=fmt_duration(s.info.duration)))
        return
    await state.clear()
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.cut(src, dst, info, start, end, job)

    label = texts.STEP_CUT.format(start=fmt_duration(start), end=fmt_duration(end))
    await execute(bot, message.chat.id, s, label, info.out_ext, run)
