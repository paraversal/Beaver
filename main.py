import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.utils import install_logs
import src.env as env
from src.admin_router import admin_router
from src.user_ticket_router import user_ticket_router

logger = logging.getLogger(__name__)

async def main() -> None:
    bot = Bot(token=env.TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(user_ticket_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    install_logs()
    asyncio.run(main())
