from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberOwner

from src.auth import AdminAuth
from src.config import Config, ConfigError
from src.db import Database

logger = logging.getLogger(__name__)


@dataclass
class App:
    config: Config
    db: Database
    bot: Bot
    bot_id: int
    support_chat: Chat
    admin_auth: AdminAuth
    bot_is_admin: bool
    # None means the inbox is the whole chat (a plain group has no topics).
    delivery_thread_id: int | None = None

    @classmethod
    async def create(cls, config: Config, bot: Bot) -> "App":
        db = Database(config.db_path)
        await db.connect()
        try:
            app = await cls._startup(config, bot, db)
        except Exception:
            await db.close()
            raise
        return app

    @classmethod
    async def _startup(cls, config: Config, bot: Bot, db: Database) -> "App":
        try:
            me = await bot.get_me()
        except Exception as exc:
            raise ConfigError(f"Telegram rejected the bot token: {exc}") from exc
        try:
            chat = await bot.get_chat(config.support_chat_id)
        except Exception as exc:
            raise ConfigError(
                f"Cannot access SUPPORT_CHAT_ID {config.support_chat_id}: {exc}"
            ) from exc

        if chat.type not in (ChatType.SUPERGROUP, ChatType.GROUP):
            raise ConfigError(
                f"SUPPORT_CHAT_ID must point at a private group, got {chat.type}"
            )
        if config.uses_topic and not chat.is_forum:
            raise ConfigError(
                "TICKETS_TOPIC_ID is set but the support group does not have Topics enabled"
            )

        try:
            member = await bot.get_chat_member(config.support_chat_id, me.id)
        except Exception as exc:
            raise ConfigError(f"Cannot check the bot's membership in the support group: {exc}") from exc
        bot_is_admin = isinstance(member, (ChatMemberAdministrator, ChatMemberOwner)) or (
            getattr(member, "status", None) in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        )
        if not bot_is_admin:
            raise ConfigError(
                "The bot must be an administrator in the support group "
                "(required for delivery, reaction-close, and cleanup)"
            )
        can_delete = isinstance(member, ChatMemberOwner) or bool(
            getattr(member, "can_delete_messages", False)
        )
        if not can_delete:
            logger.warning(
                "bot lacks the Delete Messages admin right; /reply, /close, "
                "/reopen, and /help command cleanup will be best-effort"
            )

        admin_auth = AdminAuth(bot, config, linked_channel_id=chat.linked_chat_id)

        delivery_thread_id: int | None = None
        if chat.is_forum:
            if config.uses_topic:
                delivery_thread_id = config.tickets_topic_id
            else:
                # Forum with no topic configured: General is the inbox. Never
                # follow the admin's message, or one ticket's cards would split
                # across threads.
                delivery_thread_id = 1
        # A plain group has no threads at all: delivery_thread_id stays None and
        # every ticket lives in the chat itself.
        logger.info(
            "profile=%s support_chat=%s chat_type=%s forum=%s topic=%s bot_admin=%s can_delete=%s",
            config.app_env,
            config.support_chat_id,
            chat.type,
            chat.is_forum,
            config.tickets_topic_id or "whole chat",
            bot_is_admin,
            can_delete,
        )

        return cls(
            config=config,
            db=db,
            bot=bot,
            bot_id=me.id,
            support_chat=chat,
            admin_auth=admin_auth,
            bot_is_admin=bot_is_admin,
            delivery_thread_id=delivery_thread_id,
        )

    async def close(self) -> None:
        await self.db.close()
