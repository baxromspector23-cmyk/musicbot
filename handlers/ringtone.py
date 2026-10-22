"""Ringtone maker."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import texts
from handlers.common import execute, require_session, require_session_msg
from keyboards import MenuCb, OpCb
from services import ffmpeg
from states import RingStates
from utils import fmt_duration, parse_time

router = Router(name="ringtone")


@router.callback_query(MenuCb.filter(F.action == "ring"))
async def ring_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.set_state(RingStates.waiting_start)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.RING_ASK_START.format(duration=fmt_duration(s.info.duration)),
                           reply_markup=kb.input_back(s.sid))


@router.message(RingStates.waiting_start, F.text)
async def ring_start_text(message: Message, state: FSMContext) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    try:
        start = parse_time(message.text or "")
        if start >= s.info.duration - 1:
            raise ValueError("start too late")
    except ValueError:
        await message.answer(texts.RING_BAD_START.format(duration=fmt_duration(s.info.duration)))
        return
    await state.set_state(RingStates.choosing)
    await state.update_data(start=start)
    await message.answer(texts.RING_ASK_LEN, reply_markup=kb.ring_lengths(s.sid))


@router.callback_query(OpCb.filter(F.action == "ring_len"))
async def ring_len(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if "start" not in await state.get_data():
        await cb.answer(texts.SESSION_EXPIRED, show_alert=True)
        return
    await state.update_data(length=float(callback_data.arg))
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.RING_ASK_FADE, reply_markup=kb.ring_fades(s.sid))


@router.callback_query(OpCb.filter(F.action == "ring_fade"))
async def ring_fade(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if "length" not in await state.get_data():
        await cb.answer(texts.SESSION_EXPIRED, show_alert=True)
        return
    await state.update_data(fade=float(callback_data.arg))
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.RING_ASK_TARGET, reply_markup=kb.ring_targets(s.sid))


@router.callback_query(OpCb.filter(F.action == "ring_do"))
async def ring_do(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    data = await state.get_data()
    if not {"start", "length", "fade"} <= data.keys():
        await cb.answer(texts.SESSION_EXPIRED, show_alert=True)
        return
    await cb.answer()
    await state.clear()
    start = float(data["start"])
    length = min(float(data["length"]), 30.0, s.info.duration - start)
    fade_out = min(float(data["fade"]), length / 2)
    ext = "m4r" if callback_data.arg == "iphone" else "mp3"
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.make_ringtone(src, dst, info, start, length, fade_out, job)

    await execute(bot, cb.from_user.id, s, texts.STEP_RINGTONE.format(sec=int(round(length))), ext, run)
