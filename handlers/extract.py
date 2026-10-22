"""Extract audio from video."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import keyboards as kb
import texts
from handlers.common import execute, require_session
from keyboards import MenuCb, OpCb
from services import ffmpeg

router = Router(name="extract")


@router.callback_query(MenuCb.filter(F.action == "ext"))
async def ext_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if not s.info.has_video:
        await cb.answer(texts.NO_AUDIO, show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.EXTRACT_CHOOSE, reply_markup=kb.extract_kb(s.sid))


@router.callback_query(OpCb.filter(F.action == "ext_do"))
async def ext_do(cb: CallbackQuery, callback_data: OpCb, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    mode = callback_data.arg
    src, info = s.current, s.info
    ext = ffmpeg.extract_target_ext(info, mode)

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.extract_audio(src, dst, info, mode, job)

    await execute(bot, cb.from_user.id, s, texts.STEP_EXTRACT, ext, run)
