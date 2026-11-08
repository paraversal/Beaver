from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, MessageReactionUpdated

from src.app import App
from src.tickets import TicketConflict, TicketService
from src.texts import admin_help_text, admin_text, whereami_text
from src.utils import human_age

logger = logging.getLogger(__name__)
admin_router = Router(name="admin")

TELEGRAM_TEXT_LIMIT = 4096


def _too_long(text: str) -> bool:
    return len(text) > TELEGRAM_TEXT_LIMIT


def _too_long_message(length: int) -> str:
    return (
        f"That text is too long ({length} characters; max {TELEGRAM_TEXT_LIMIT}). "
        "Nothing was sent; shorten it and try again."
    )


def _in_support_topic(message: Message, app: App) -> bool:
    if message.chat.id != app.config.support_chat_id:
        return False
    if app.config.uses_topic:
        return message.message_thread_id == app.config.tickets_topic_id
    if app.support_chat.is_forum:
        # No topic configured: the inbox is General (thread id 1 / None).
        return message.message_thread_id in (None, 1)
    # Plain group: the whole chat is the inbox.
    return True


async def _reply(message: Message, text: str) -> None:
    try:
        await message.answer(text, link_preview_options=None)
    except Exception:
        logger.exception("could not send admin reply")


def _in_support_chat(message: Message, app: App) -> bool:
    return message.chat.id == app.config.support_chat_id


async def _delete_admin_message(message: Message, app: App) -> None:
    try:
        await app.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except TelegramAPIError:
        logger.warning("could not delete admin command message %s", message.message_id)


async def _admin_notice(message: Message, app: App, text: str) -> None:
    """DM an admin, then remove their command from the group.

    If the DM fails (or the sender is anonymous), keep the message and reply in
    the group so the admin still sees the feedback.
    """
    user = message.from_user
    if user is None:
        await _reply(message, text)
        return
    try:
        await app.bot.send_message(chat_id=user.id, text=text)
    except TelegramAPIError:
        logger.warning("could not DM admin %s; replying in the group", user.id)
        await _reply(message, text)
        return
    await _delete_admin_message(message, app)


@admin_router.message(Command("help"), F.chat.type != ChatType.PRIVATE)
async def admin_help(message: Message, app: App) -> None:
    if message.chat.id != app.config.support_chat_id:
        return
    if not await app.admin_auth.is_admin_message(message):
        return
    await _admin_notice(message, app, admin_help_text())


@admin_router.message(Command("whereami"))
async def whereami(message: Message, app: App) -> None:
    if not await app.admin_auth.is_admin_message(message):
        return
    thread_id = message.message_thread_id
    await _reply(
        message,
        whereami_text(
            app.config,
            chat=message.chat,
            is_support_forum=app.support_chat.is_forum,
            thread_id=thread_id,
            bot_is_admin=app.bot_is_admin,
            sender_is_admin=True,
        ),
    )


@admin_router.message(Command("open"))
async def open_command(message: Message, app: App) -> None:
    if message.chat.type == ChatType.PRIVATE:
        if not await app.admin_auth.is_admin_message(message):
            return
    elif not _in_support_topic(message, app):
        return
    elif not await app.admin_auth.is_admin_message(message):
        return

    page = 1
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            try:
                page = max(1, int(parts[1].strip()))
            except ValueError:
                page = 1
    service = TicketService(app)
    items = await service.list_open()
    await _reply(message, service.render_open_list(items, page))


async def _ticket_from_reply(message: Message, app: App):
    if message.reply_to_message is None:
        await _admin_notice(
            message,
            app,
            "Use this command as a reply to the ticket card or one of its messages.",
        )
        return None
    service = TicketService(app)
    ticket, _direction = await service.resolve_ticket_reply(message)
    if ticket is None:
        await _admin_notice(message, app, "That message is not part of a ticket.")
        return None
    return ticket


def _command_argument(message: Message) -> str:
    if not message.text:
        return ""
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@admin_router.message(Command("reply"), F.chat.type != ChatType.PRIVATE)
async def reply_command(message: Message, app: App) -> None:
    if not _in_support_chat(message, app):
        return
    if not await app.admin_auth.is_admin_message(message):
        return
    if not _in_support_topic(message, app):
        await _admin_notice(message, app, "Use /reply in the ticket topic.")
        return
    text = _command_argument(message)
    if not text:
        await _admin_notice(
            message,
            app,
            "Use /reply <text> as a reply to the ticket card.",
        )
        return
    delivered = admin_text(text)
    if _too_long(delivered):
        await _admin_notice(message, app, _too_long_message(len(delivered)))
        return
    ticket = await _ticket_from_reply(message, app)
    if ticket is None:
        return
    await TicketService(app).send_admin_reply(
        message,
        ticket,
        text,
        sender_user_id=message.from_user.id if message.from_user else None,
    )


@admin_router.message(Command("close"), F.chat.type != ChatType.PRIVATE)
async def close_command(message: Message, app: App) -> None:
    if not _in_support_chat(message, app):
        return
    if not await app.admin_auth.is_admin_message(message):
        return
    if not _in_support_topic(message, app):
        await _admin_notice(message, app, "Use /close in the ticket topic.")
        return
    ticket = await _ticket_from_reply(message, app)
    if ticket is None:
        return
    if not ticket.is_open:
        await _admin_notice(
            message,
            app,
            f"#{ticket.id} is already closed. Use /reopen to open it again.",
        )
        return
    closing_text = _command_argument(message) or None
    if closing_text and _too_long(admin_text(closing_text)):
        await _admin_notice(
            message,
            app,
            f"That closing text is too long ({len(admin_text(closing_text))} characters; "
            f"max {TELEGRAM_TEXT_LIMIT}). The ticket was not closed.",
        )
        return
    service = TicketService(app)
    updated = await service.close(
        ticket,
        by_user_id=message.from_user.id if message.from_user else None,
        closing_text=closing_text,
        by_admin=True,
    )
    await app.db.add_ticket_message(
        ticket_id=updated.id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        direction="admin_reply" if closing_text else "system",
        sender_user_id=message.from_user.id if message.from_user else None,
        parent_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
    )
    await _delete_admin_message(message, app)


@admin_router.message(Command("reopen"), F.chat.type != ChatType.PRIVATE)
async def reopen_command(message: Message, app: App) -> None:
    if not _in_support_chat(message, app):
        return
    if not await app.admin_auth.is_admin_message(message):
        return
    if not _in_support_topic(message, app):
        await _admin_notice(message, app, "Use /reopen in the ticket topic.")
        return
    ticket = await _ticket_from_reply(message, app)
    if ticket is None:
        return
    if ticket.is_open:
        await _admin_notice(message, app, f"#{ticket.id} is already open.")
        return
    service = TicketService(app)
    try:
        await service.reopen(ticket, notify_user=True)
    except TicketConflict as exc:
        await _admin_notice(
            message,
            app,
            f"#{exc.ticket_id} was not reopened: this user already has open "
            f"ticket #{exc.open_ticket_id}.",
        )
        return
    await _delete_admin_message(message, app)


@admin_router.message(Command("tickets_info"))
async def tickets_info(message: Message, app: App) -> None:
    """Small operator aid; not advertised to users."""
    if not await app.admin_auth.is_admin_message(message):
        return
    items = await TicketService(app).list_open()
    total = len(items)
    oldest = human_age(items[0].ticket.last_activity_at) if items else "—"
    await _reply(message, f"Open tickets: {total}\nOldest activity: {oldest}")


@admin_router.message_reaction()
async def reaction_close(update: MessageReactionUpdated, app: App) -> None:
    if update.chat.id != app.config.support_chat_id:
        return
    if not await app.admin_auth.is_admin_reaction(update):
        return
    await TicketService(app).close_from_reaction(update)
