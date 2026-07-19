from aiogram.filters.command import Command
from aiogram import html
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.dispatcher.router import Router
from aiogram.types.reply_keyboard_remove import ReplyKeyboardRemove
from logging import getLogger

from src.utils import get_full_name_or_unknown, secret_code_is_correct, TEXTS
import src.env as env

logger = getLogger(__name__)

class Ticket(StatesGroup):
    secret = State()
    trusted = State()
    concern = State()

user_ticket_router = Router(name="user ticket router")

# entry point -> user begins flow
@user_ticket_router.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    """
    This handler receives messages with `/start` command
    """
    await state.set_state(Ticket.secret)
    user_name = get_full_name_or_unknown(message)
    await message.answer(TEXTS["welcome"](user_name),reply_markup=ReplyKeyboardRemove())

@user_ticket_router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    """
    Allow user to cancel any action
    """
    current_state = await state.get_state()
    if current_state is None:
        return

    logger.info("Cancelling state %r", current_state)
    await state.clear()
    await message.answer(
        "Cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )

@user_ticket_router.message(Ticket.secret)
async def secret_pass(message: Message, state: FSMContext) -> None:
    is_trusted = False
    await state.update_data(secret=message.text)
    if message.text and secret_code_is_correct(message.text):
        is_trusted = True
        await state.set_state(Ticket.concern)
        await message.reply(TEXTS["secret_correct"], reply_markup=ReplyKeyboardRemove())
    else:
        logger.info(f"untrusted: {message.text} by user {message.from_user.id}")
    await state.update_data(trusted = is_trusted)


@user_ticket_router.message(Ticket.concern)
async def concern_pass(message: Message, state: FSMContext) -> None:
    await state.update_data(concern=message.text)
    await message.reply("Thanks! We will get back to you ASAP! 🦦")
    assert message.bot is not None, "Bot instance cannot be retrieved."
    assert env.SUPERGROUP_ID is not None, "No supergroup ID assigned."
    await message.bot.send_message(chat_id=env.SUPERGROUP_ID, text=f"{html.bold("New ticket!")} by {message.from_user.full_name}\n----\n\n{message.text}")
