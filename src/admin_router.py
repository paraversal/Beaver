from logging import getLogger

from aiogram import F
from aiogram.types import Message
from aiogram.dispatcher.router import Router

import src.env as env

logger = getLogger(__name__)

admin_router = Router(name="admin router")

@admin_router.message(F.chat.func(lambda chat: str(chat.id) == env.SUPERGROUP_ID))
async def reply_to_ticket(message: Message) -> None:
    print("TODO: this is the routine for a ticket reply")

@admin_router.message((F.from_user.id.in_(env.ADMIN_IDS)) & (F.text == '/open'))
async def all_open(message: Message):
    print("TODO: this should return all open tickets")
