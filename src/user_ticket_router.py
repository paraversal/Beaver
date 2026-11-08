from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatType
from aiogram.types import Message

from src.app import App
from src.db import Ticket, User
from src.tickets import SupportUnavailable, TicketService
from src.texts import (
    challenge_text,
    status_text,
    ticket_closed_text,
    unknown_command_text,
    verified_text,
    wrong_secret_text,
)
from src.utils import normalize_secret, parse_timestamp

logger = logging.getLogger(__name__)
user_ticket_router = Router(name="user_ticket")


def _is_private(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def _upsert_user(message: Message, app: App) -> User:
    assert message.from_user is not None
    return await app.db.upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        language_code=message.from_user.language_code,
    )


async def _open_ticket(app: App, user_id: int) -> Ticket | None:
    return await app.db.get_open_ticket_for_user(user_id)


@user_ticket_router.message(F.chat.type == ChatType.PRIVATE, CommandStart())
async def start(message: Message, app: App) -> None:
    user = await _upsert_user(message, app)
    if user.is_verified:
        await message.answer(status_text(await _open_ticket(app, user.user_id)))
        return
    await message.answer(challenge_text(app.config.secret_question))


@user_ticket_router.message(F.chat.type == ChatType.PRIVATE, Command("help"))
async def help_command(message: Message, app: App) -> None:
    await message.answer(
        "This bot connects you to support.\n\n"
        "• Send a message, photo, or file to open a ticket.\n"
        "• Keep writing here to add details to the same ticket.\n"
        "• /status shows your open ticket.\n"
        "• /close marks it resolved; reply within "
        f"{app.config.reopen_window_days} days to reopen it.\n"
        "• /new starts a fresh ticket; /new <issue> opens it immediately."
    )


@user_ticket_router.message(F.chat.type == ChatType.PRIVATE, Command("status"))
async def status_command(message: Message, app: App) -> None:
    user = await _upsert_user(message, app)
    await message.answer(status_text(await _open_ticket(app, user.user_id)))


@user_ticket_router.message(F.chat.type == ChatType.PRIVATE, Command("close"))
async def close_command(message: Message, app: App) -> None:
    user = await _upsert_user(message, app)
    ticket = await _open_ticket(app, user.user_id)
    if ticket is None:
        await message.answer("You have no open ticket.")
        return
    await TicketService(app).close(ticket, by_user_id=user.user_id, notify_user=False)
    await message.answer(ticket_closed_text(ticket.id, app.config.reopen_window_days))


def _new_argument(message: Message) -> str:
    if not message.text:
        return ""
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@user_ticket_router.message(F.chat.type == ChatType.PRIVATE, Command("new"))
async def new_command(message: Message, app: App) -> None:
    user = await _upsert_user(message, app)
    if await _open_ticket(app, user.user_id) is not None:
        await message.answer("You already have an open ticket. Use /close first, then /new.")
        return
    if not user.is_verified:
        await message.answer(challenge_text(app.config.secret_question))
        return
    text = _new_argument(message)
    if text:
        try:
            await TicketService(app).create_new(message, user, text)
        except SupportUnavailable:
            await message.answer(
                "Support is not configured right now. Please try again later."
            )
        except Exception:
            logger.exception("failed to create ticket from /new")
            await message.answer("Something went wrong on our side. Please try again in a moment.")
        return
    await app.db.set_force_new(
        user.user_id,
        (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    await message.answer("Send your new issue and I'll open a fresh ticket.")


def _oversize(message: Message, app: App) -> bool:
    limit = app.config.max_ticket_file_mb * 1024 * 1024
    for attribute in ("document", "video", "audio", "voice", "video_note", "animation"):
        item = getattr(message, attribute, None)
        size = getattr(item, "file_size", None)
        if size and size > limit:
            return True
    return False


@user_ticket_router.message(F.chat.type == ChatType.PRIVATE)
async def on_user_message(message: Message, app: App) -> None:
    user = await _upsert_user(message, app)

    if user.is_verified:
        if message.text and message.text.startswith("/"):
            await message.answer(unknown_command_text())
            return
        if _oversize(message, app):
            await message.answer(
                f"That file is too large. Please send something under "
                f"{app.config.max_ticket_file_mb} MB or a link."
            )
            return
        try:
            await TicketService(app).open_or_append(message, user)
        except SupportUnavailable:
            await message.answer(
                "Support is not configured right now. Please try again later."
            )
        except Exception:
            logger.exception("failed to handle user message")
            await message.answer("Something went wrong on our side. Please try again in a moment.")
        return

    # Unverified: the only useful input is a text answer.
    if not message.text:
        await message.answer("Please answer the question with a text message.")
        return
    if message.text.startswith("/"):
        await message.answer(challenge_text(app.config.secret_question))
        return

    now = datetime.now(timezone.utc)
    cooldown = parse_timestamp(user.cooldown_until)
    if cooldown is not None and cooldown > now:
        await message.answer("Too many attempts. Please wait a little and try again.")
        return
    if cooldown is not None:
        await app.db.set_failed_attempts(user.user_id, 0, None)
        user = await app.db.get_user(user.user_id) or user

    if normalize_secret(message.text) in app.config.secrets:
        await app.db.set_verified(user.user_id)
        await message.answer(verified_text())
        return

    attempts = user.failed_attempts + 1
    if attempts >= app.config.verify_max_attempts:
        cooldown_until = (
            now + timedelta(minutes=app.config.verify_cooldown_min)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        await app.db.set_failed_attempts(user.user_id, attempts, cooldown_until)
        await message.answer(
            f"That's not it. Too many attempts; try again in "
            f"{app.config.verify_cooldown_min} minutes."
        )
        return

    await app.db.set_failed_attempts(user.user_id, attempts, None)
    await message.answer(wrong_secret_text(app.config.verify_max_attempts - attempts))
