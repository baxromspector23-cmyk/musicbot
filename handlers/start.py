"""/start, /help, /new."""
from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import texts
from services import session as store

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.START)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    """End the current session."""
    await state.clear()
    store.end_session(message.from_user.id)
    await message.answer(texts.NEW_SESSION)
