from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.types import Chat, Message, MessageReactionUpdated

from src.config import Config

logger = logging.getLogger(__name__)


class AdminAuth:
    """Admin check backed by Telegram's group administrator list.

    `ADMIN_IDS` is a bootstrap/owner allowlist. Everyone else is checked against
    the support group's current administrators, cached briefly so a support
    conversation does not call getChatAdministrators on every message.
    """

    def __init__(
        self,
        bot: Bot,
        config: Config,
        linked_channel_id: int | None = None,
    ) -> None:
        self.bot = bot
        self.config = config
        self.linked_channel_id = linked_channel_id
        self._administrators: set[int] = set()
        # None = never loaded. (A 0.0 sentinel breaks right after boot:
        # time.monotonic() counts from boot, so for the first TTL seconds the
        # cache looked fresh and group admins were not recognized.)
        self._loaded_at: float | None = None
        self._ttl = 300.0

    async def refresh(self) -> None:
        try:
            members = await self.bot.get_chat_administrators(self.config.support_chat_id)
        except Exception:
            logger.exception("could not load support group administrators")
            return
        self._administrators = {
            member.user.id
            for member in members
            if getattr(member, "user", None) is not None
        }
        self._loaded_at = time.monotonic()

    async def _ensure_loaded(self) -> None:
        if self._loaded_at is None or time.monotonic() - self._loaded_at > self._ttl:
            await self.refresh()

    def _is_support_chat(self, chat: Chat | None) -> bool:
        if chat is None:
            return False
        return chat.id in {self.config.support_chat_id, self.linked_channel_id}

    async def is_admin_user(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if user_id in self.config.admin_ids:
            return True
        await self._ensure_loaded()
        return user_id in self._administrators

    async def is_admin_message(self, message: Message) -> bool:
        if self._is_support_chat(message.sender_chat):
            return True
        return await self.is_admin_user(message.from_user.id if message.from_user else None)

    async def is_admin_reaction(self, update: MessageReactionUpdated) -> bool:
        if self._is_support_chat(update.actor_chat):
            return True
        return await self.is_admin_user(update.user.id if update.user else None)
