import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram import F
from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.router import Router
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.filters.command import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.types.reply_keyboard_remove import ReplyKeyboardRemove
from src.utils import install_logs, secret_code_is_correct, TEXTS
import src.env as env


TOKEN = getenv("BOT_TOKEN")
class Ticket(StatesGroup):
    secret = State()
    trusted = State()
    concern = State()

user_ticket_router = Router(name="user ticket router")
admin_router = Router(name="admin router")

@admin_router.message(F.chat.func(lambda chat: str(chat.id) == env.SUPERGROUP_ID))
async def reply_to_ticket(message: Message) -> None:
    print(message.chat.id)
    print(f"uuuuhhhh, replied {message.text}")

# entry point -> user begins flow
@user_ticket_router.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    """
    This handler receives messages with `/start` command
    """
    await state.set_state(Ticket.secret)
    await message.answer(TEXTS["welcome"](message.from_user.full_name),reply_markup=ReplyKeyboardRemove())

@user_ticket_router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    """
    Allow user to cancel any action
    """
    current_state = await state.get_state()
    if current_state is None:
        return

    logging.info("Cancelling state %r", current_state)
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
        logger.info(f"untrusted: {message.text} by user {message.from_user.full_name}")
    await state.update_data(trusted = is_trusted)


@user_ticket_router.message(Ticket.concern)
async def concern_pass(message: Message, state: FSMContext) -> None:
    await state.update_data(concern=message.text)
    await message.reply("Thanks! We will get back to you ASAP! 🦦")
    assert message.bot is not None, "Bot instance cannot be retrieved."
    assert env.SUPERGROUP_ID is not None, "No supergroup ID assigned."
    await message.bot.send_message(chat_id=env.SUPERGROUP_ID, text=f"{html.bold("New ticket!")} by {message.from_user.full_name}\n----\n\n{message.text}")

async def main() -> None:
    bot = Bot(token=env.TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(user_ticket_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    install_logs()
    asyncio.run(main())
