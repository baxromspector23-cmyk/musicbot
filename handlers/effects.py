"""Fade, speed, pitch and volume handlers."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import texts
from handlers.common import execute, require_session, require_session_msg
from keyboards import MenuCb, OpCb
from services import ffmpeg
from services.session import Session
from states import FadeStates, PitchStates, SpeedStates, VolumeStates
from utils import fade_limit, fmt_num, parse_float, parse_int, parse_volume

router = Router(name="effects")


# ---------------------------------------------------------------- fade

@router.callback_query(MenuCb.filter(F.action == "fade"))
async def fade_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.clear()
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.FADE_CHOOSE, reply_markup=kb.fade_modes(s.sid))


@router.callback_query(OpCb.filter(F.action == "fade_mode"))
async def fade_mode(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.set_state(FadeStates.waiting_seconds)
    await state.update_data(mode=callback_data.arg)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.FADE_SECONDS, reply_markup=kb.fade_seconds(s.sid))


async def _run_fade(bot: Bot, chat_id: int, s: Session, state: FSMContext, d: float) -> None:
    mode = (await state.get_data()).get("mode", "in")
    limit = fade_limit(mode, s.info.duration)
    if not 0 < d <= limit:
        await bot.send_message(chat_id, texts.FADE_BAD.format(limit=fmt_num(limit)))
        return
    await state.clear()
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.fade(src, dst, info, mode, d, job)

    names = {"in": "in", "out": "out", "both": "in+out"}
    await execute(bot, chat_id, s, texts.STEP_FADE.format(mode=names[mode], sec=fmt_num(d)), info.out_ext, run)


@router.callback_query(OpCb.filter(F.action == "fade_sec"))
async def fade_sec(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    await _run_fade(bot, cb.from_user.id, s, state, float(callback_data.arg))


@router.message(FadeStates.waiting_seconds, F.text)
async def fade_text(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    try:
        d = parse_float(message.text or "", 0.001, 3600)
    except ValueError:
        mode = (await state.get_data()).get("mode", "in")
        await message.answer(texts.FADE_BAD.format(limit=fmt_num(fade_limit(mode, s.info.duration))))
        return
    await _run_fade(bot, message.chat.id, s, state, d)


# ---------------------------------------------------------------- speed

@router.callback_query(MenuCb.filter(F.action == "speed"))
async def speed_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.set_state(SpeedStates.waiting_value)
    await state.update_data(keep_pitch=True)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.SPEED_ASK, reply_markup=kb.speed_kb(s.sid, True))


@router.callback_query(OpCb.filter(F.action == "speed_toggle"))
async def speed_toggle(cb: CallbackQuery, callback_data: OpCb, state: FSMContext) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    keep = not (await state.get_data()).get("keep_pitch", True)
    await state.set_state(SpeedStates.waiting_value)
    await state.update_data(keep_pitch=keep)
    await cb.answer()
    if cb.message is not None:
        try:
            await cb.message.edit_reply_markup(reply_markup=kb.speed_kb(s.sid, keep))
        except Exception:
            pass


async def _run_speed(bot: Bot, chat_id: int, s: Session, state: FSMContext, factor: float) -> None:
    keep = (await state.get_data()).get("keep_pitch", True)
    await state.clear()
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.change_speed(src, dst, info, factor, keep, job)

    tpl = texts.STEP_SPEED if keep else texts.STEP_SPEED_PITCH
    await execute(bot, chat_id, s, tpl.format(factor=fmt_num(factor)), info.out_ext, run)


@router.callback_query(OpCb.filter(F.action == "speed_set"))
async def speed_set(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    await _run_speed(bot, cb.from_user.id, s, state, float(callback_data.arg))


@router.message(SpeedStates.waiting_value, F.text)
async def speed_text(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    try:
        factor = parse_float(message.text or "", 0.5, 2.0)
    except ValueError:
        await message.answer(texts.SPEED_BAD)
        return
    await _run_speed(bot, message.chat.id, s, state, factor)


# ---------------------------------------------------------------- pitch

@router.callback_query(MenuCb.filter(F.action == "pitch"))
async def pitch_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.set_state(PitchStates.waiting_value)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.PITCH_ASK, reply_markup=kb.pitch_kb(s.sid))


async def _run_pitch(bot: Bot, chat_id: int, s: Session, state: FSMContext, n: int) -> None:
    await state.clear()
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.shift_pitch(src, dst, info, n, job)

    await execute(bot, chat_id, s, texts.STEP_PITCH.format(n=n), info.out_ext, run)


@router.callback_query(OpCb.filter(F.action == "pitch_set"))
async def pitch_set(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    await _run_pitch(bot, cb.from_user.id, s, state, int(callback_data.arg))


@router.message(PitchStates.waiting_value, F.text)
async def pitch_text(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    try:
        n = parse_int(message.text or "", -12, 12)
    except ValueError:
        await message.answer(texts.PITCH_BAD)
        return
    await _run_pitch(bot, message.chat.id, s, state, n)


# ---------------------------------------------------------------- volume / normalize

@router.callback_query(MenuCb.filter(F.action == "vol"))
async def vol_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.set_state(VolumeStates.waiting_value)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.VOLUME_ASK, reply_markup=kb.volume_kb(s.sid))


async def _run_volume(bot: Bot, chat_id: int, s: Session, state: FSMContext, factor: float) -> None:
    await state.clear()
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.change_volume(src, dst, info, factor, job)

    await execute(bot, chat_id, s, texts.STEP_VOLUME.format(factor=fmt_num(factor)), info.out_ext, run)


@router.callback_query(OpCb.filter(F.action == "vol_set"))
async def vol_set(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    await _run_volume(bot, cb.from_user.id, s, state, parse_volume(callback_data.arg))


@router.message(VolumeStates.waiting_value, F.text)
async def vol_text(message: Message, state: FSMContext, bot: Bot) -> None:
    s = await require_session_msg(message, state)
    if s is None:
        return
    try:
        factor = parse_volume(message.text or "")
    except ValueError:
        await message.answer(texts.VOLUME_BAD)
        return
    await _run_volume(bot, message.chat.id, s, state, factor)


@router.callback_query(OpCb.filter(F.action == "norm_menu"))
async def norm_menu(cb: CallbackQuery, callback_data: OpCb, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.NORMALIZE_CHOOSE, reply_markup=kb.normalize_kb(s.sid))


@router.callback_query(OpCb.filter(F.action == "norm_set"))
async def norm_set(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    lufs = float(callback_data.arg)
    await state.clear()
    await cb.answer()
    src, info = s.current, s.info

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.normalize(src, dst, info, lufs, job)

    await execute(bot, cb.from_user.id, s, texts.STEP_NORMALIZE.format(lufs=int(lufs)), info.out_ext, run)
