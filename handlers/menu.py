"""Main menu navigation."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from handlers.common import require_session, send_menu
from keyboards import MenuCb

router = Router(name="menu")


@router.callback_query(MenuCb.filter(F.action == "menu"))
async def show_menu(cb: CallbackQuery, callback_data: MenuCb, state: FSMContext, bot: Bot) -> None:
    """Back to the main menu for the current file."""
    s = await require_session(cb, callback_data.sid)
    if s is None:
        return
    await state.clear()
    await cb.answer()
    await send_menu(bot, cb.from_user.id, s)
