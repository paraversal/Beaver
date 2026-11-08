from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChatAdministrators,
    ErrorEvent,
)

from src.admin_router import admin_router
from src.app import App
from src.config import Config, ConfigError
from src.user_ticket_router import user_ticket_router
from src.utils import install_logs

logger = logging.getLogger(__name__)


USER_COMMANDS = (
    BotCommand(command="start", description="Start or show your ticket"),
    BotCommand(command="help", description="How support works"),
    BotCommand(command="status", description="Show your open ticket"),
    BotCommand(command="close", description="Close your ticket"),
    BotCommand(command="new", description="New ticket; add the issue to open it now"),
)

ADMIN_COMMANDS = (
    BotCommand(command="help", description="Admin guide"),
    BotCommand(command="reply", description="Reply to the ticket user (folds into the card)"),
    BotCommand(command="close", description="Close the ticket (optional final text)"),
    BotCommand(command="reopen", description="Reopen a closed ticket"),
    BotCommand(command="open", description="List open tickets"),
    BotCommand(command="whereami", description="Show chat/topic IDs"),
)


async def _set_commands(bot: Bot, config: Config) -> None:
    """Set separate menus: user commands in DMs, admin commands in the group.

    The default scope is cleared, and group chats explicitly get no commands,
    so user commands never appear in the support group or any other group. The
    support chat then gets its own admin menu, which overrides the empty group
    scope. Telegram command scopes are chat-level, so all topics of a forum see
    the same admin menu; handlers still enforce the ticket topic.
    """
    try:
        await bot.delete_my_commands()
    except Exception:
        logger.exception("could not clear the default command menu")
    try:
        await bot.set_my_commands(
            list(USER_COMMANDS),
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception:
        logger.exception("could not set the private-chat command menu")
    try:
        await bot.set_my_commands(
            [],
            scope=BotCommandScopeAllGroupChats(),
        )
    except Exception:
        logger.exception("could not clear the group command menus")
    try:
        await bot.set_my_commands(
            list(ADMIN_COMMANDS),
            scope=BotCommandScopeChatAdministrators(chat_id=config.support_chat_id),
        )
    except Exception:
        logger.exception("could not set the support-group command menu")


async def run(env_file: str | None, clear_staging_tickets: bool = False) -> int:
    try:
        config = Config.from_env(env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if clear_staging_tickets and config.app_env == "production":
        print(
            "Refusing --clear-staging-tickets when APP_ENV=production. "
            "Use APP_ENV=staging or development.",
            file=sys.stderr,
        )
        return 2

    install_logs(config.log_level)
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    app: App | None = None
    try:
        try:
            app = await App.create(config, bot)
        except ConfigError as exc:
            print(f"Startup error: {exc}", file=sys.stderr)
            return 2
        if clear_staging_tickets:
            await app.db.clear_tickets()
        await _set_commands(bot, config)

        dispatcher = Dispatcher()
        dispatcher["app"] = app

        @dispatcher.errors()
        async def on_error(event: ErrorEvent) -> bool:
            logger.error(
                "update failed: %s",
                event.exception,
                exc_info=event.exception,
            )
            return True

        dispatcher.include_router(admin_router)
        dispatcher.include_router(user_ticket_router)

        await dispatcher.start_polling(
            bot,
            allowed_updates=["message", "message_reaction"],
        )
        return 0
    finally:
        if app is not None:
            await app.close()
        await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Beaver Telegram support bot")
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "Env file to load; it must exist if given. Without it, .env is "
            "loaded when present, otherwise only real environment variables "
            "are used (Docker). Use .env.staging for tests."
        ),
    )
    parser.add_argument(
        "--clear-staging-tickets",
        action="store_true",
        help=(
            "Delete all tickets and ticket messages from the configured DB, "
            "then start polling; only allowed when APP_ENV is staging or development"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(run(args.env_file, args.clear_staging_tickets))


if __name__ == "__main__":
    raise SystemExit(main())
