"""Merge handler: collect files, choose join type, run."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import texts
from handlers.common import execute, require_session, send_menu
from keyboards import MenuCb, OpCb
from services import ffmpeg, intake
from services import session as store
from services.session import MergeItem, Session
from services.tags import Tags
from states import MergeStates
from utils import UserError, escape_html, fmt_duration

log = logging.getLogger(__name__)

router = Router(name="merge")


def _list_text(s: Session) -> str:
    items = "\n".join(
        texts.MERGE_ITEM.format(n=i, name=escape_html(m.name), duration=fmt_duration(m.info.duration))
        for i, m in enumerate(s.merge_files, 1)
    )
    return texts.MERGE_LIST.format(items=items)


async def _show_list(bot: Bot, chat_id: int, s: Session) -> None:
    await bot.send_message(chat_id, _list_text(s), reply_markup=kb.merge_kb(s.sid, len(s.merge_files)))


@router.callback_query(MenuCb.filter(F.action == "merge"))
async def merge_start(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    store.clear_merge(s)
    s.mode = "merge"
    s.merge_files = [MergeItem(s.current, s.info, s.source_name)]
    await state.set_state(MergeStates.collecting)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.MERGE_START.format(max=config.MAX_MERGE_FILES))
    await _show_list(bot, cb.from_user.id, s)


async def add_file(bot: Bot, message: Message, s: Session, inc: intake.Incoming, state: FSMContext) -> None:
    """Download, probe and queue a file received in merge mode."""
    if len(s.merge_files) >= config.MAX_MERGE_FILES:
        await message.answer(texts.MERGE_LIMIT.format(max=config.MAX_MERGE_FILES))
        return
    dest = s.work_dir / f"m{len(s.merge_files)}_{s.version}_{inc.file_id[-6:]}.{inc.ext}"
    try:
        info = await intake.fetch(bot, inc, dest)
    except UserError as exc:
        dest.unlink(missing_ok=True)
        await message.answer(str(exc))
        return
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    s.merge_files.append(MergeItem(dest, info, inc.stem))
    await state.set_state(MergeStates.collecting)
    await _show_list(bot, message.chat.id, s)


@router.message(MergeStates.collecting, F.text)
async def merge_text(message: Message) -> None:
    await message.answer(texts.MERGE_TEXT_HINT)


@router.callback_query(OpCb.filter(F.action == "merge_del"))
async def merge_del(cb: CallbackQuery, callback_data: OpCb, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if len(s.merge_files) <= 1:
        await cb.answer(texts.MERGE_CANT_DELETE, show_alert=True)
        return
    item = s.merge_files.pop()
    item.path.unlink(missing_ok=True)
    await cb.answer()
    await _show_list(bot, cb.from_user.id, s)


@router.callback_query(OpCb.filter(F.action == "merge_cancel"))
async def merge_cancel(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    store.clear_merge(s)
    await state.clear()
    await cb.answer()
    await send_menu(bot, cb.from_user.id, s, texts.CANCELLED)


@router.callback_query(OpCb.filter(F.action == "merge_go"))
async def merge_go(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if len(s.merge_files) < 2:
        await cb.answer(texts.MERGE_NEED_TWO, show_alert=True)
        return
    await state.set_state(MergeStates.choosing_join)
    await cb.answer()
    await bot.send_message(cb.from_user.id, texts.MERGE_JOIN, reply_markup=kb.join_kb(s.sid))


@router.callback_query(OpCb.filter(F.action == "merge_join"))
async def merge_join(cb: CallbackQuery, callback_data: OpCb, state: FSMContext, bot: Bot) -> None:
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    if len(s.merge_files) < 2:
        await cb.answer(texts.MERGE_NEED_TWO, show_alert=True)
        return
    crossfade = callback_data.arg == "xfade"
    if crossfade and any(m.info.duration < 2 * config.CROSSFADE_SEC for m in s.merge_files):
        await cb.answer(texts.CROSSFADE_TOO_SHORT.format(min=2 * config.CROSSFADE_SEC), show_alert=True)
        return
    await cb.answer()
    items = [(m.path, m.info) for m in s.merge_files]
    first_title = s.tags.title or s.source_name
    new_tags = Tags(f"{first_title} (mix)", s.tags.artist, s.tags.album)
    n = len(items)

    async def run(dst, job):  # type: ignore[no-untyped-def]
        await ffmpeg.merge(items, dst, crossfade, job)

    await state.clear()
    try:
        await execute(bot, cb.from_user.id, s, texts.STEP_MERGE.format(n=n), "mp3", run, new_tags=new_tags)
    finally:
        store.clear_merge(s)
