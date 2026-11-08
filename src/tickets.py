from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from aiogram import Bot, html
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    LinkPreviewOptions,
    Message,
    MessageReactionUpdated,
    ReactionTypeEmoji,
    ReplyParameters,
)

from src.app import App
from src.db import Ticket, TicketWithUser, User
from src.texts import (
    admin_closed_card_text,
    admin_closed_without_message_text,
    admin_delivery_error,
    admin_text,
    card_text,
    esc,
    open_list_text,
    preview_for_message,
    ticket_closed_text,
    ticket_opened_text,
)
from src.utils import format_timestamp, parse_timestamp, truncate, utc_now

logger = logging.getLogger(__name__)

CHECK_EMOJI = {"✅", "☑️"}


class SupportUnavailable(RuntimeError):
    """The configured support destination cannot accept this ticket."""


@dataclass(frozen=True)
class UserMessageResult:
    ticket: Ticket
    created: bool = False
    reopened: bool = False


class TicketService:
    def __init__(self, app: App) -> None:
        self.app = app
        self.bot: Bot = app.bot
        self.config = app.config
        self.db = app.db

    # -- user side -----------------------------------------------------

    async def open_or_append(
        self,
        message: Message,
        user: User,
        *,
        force_new: bool = False,
    ) -> UserMessageResult:
        open_ticket = await self.db.get_open_ticket_for_user(user.user_id)
        if open_ticket is not None:
            await self._append_user_update(message, open_ticket, user)
            return UserMessageResult(open_ticket)

        if not force_new and await self._should_reopen(user):
            closed = await self.db.get_last_closed_ticket(user.user_id)
            assert closed is not None
            await self.db.reopen_ticket(closed.id)
            ticket = await self._require_ticket(closed.id)
            await self.edit_card(ticket)
            await self._append_user_update(message, ticket, user)
            await self._send_user_message(
                user.user_id,
                f"Ticket #{ticket.id} reopened. Support will reply here.",
            )
            return UserMessageResult(ticket, reopened=True)

        ticket = await self._create_ticket(message, user)
        await self.db.set_force_new(user.user_id, None)
        return UserMessageResult(ticket, created=True)

    async def _should_reopen(self, user: User) -> bool:
        force_new_until = parse_timestamp(user.force_new_until)
        if force_new_until and force_new_until > datetime.now(timezone.utc):
            return False
        closed = await self.db.get_last_closed_ticket(user.user_id)
        if closed is None or not closed.closed_at:
            return False
        closed_at = parse_timestamp(closed.closed_at)
        if closed_at is None:
            return False
        return datetime.now(timezone.utc) <= closed_at + timedelta(
            days=self.config.reopen_window_days
        )

    async def create_new(self, message: Message, user: User, text: str) -> UserMessageResult:
        """Create a fresh ticket from an inline ``/new <issue>`` command."""
        ticket = await self._create_ticket(message, user, text=text)
        await self.db.set_force_new(user.user_id, None)
        return UserMessageResult(ticket, created=True)

    async def _create_ticket(
        self, message: Message, user: User, *, text: str | None = None
    ) -> Ticket:
        ticket_id = await self.db.create_ticket(
            user_id=user.user_id,
            preview=text if text is not None else preview_for_message(message),
            origin_chat_id=message.chat.id,
            origin_message_id=message.message_id,
            support_chat_id=self.config.support_chat_id,
            topic_id=self.app.delivery_thread_id or 0,
        )
        ticket = await self._require_ticket(ticket_id)

        try:
            card = await self.bot.send_message(
                chat_id=ticket.supergroup_chat_id,
                message_thread_id=self.app.delivery_thread_id,
                text=card_text(ticket, user),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as exc:
            await self.db.delete_ticket(ticket_id)
            logger.exception("could not send ticket card to the support chat")
            raise SupportUnavailable("the support chat rejected the ticket card") from exc

        await self.db.set_ticket_root(ticket_id, card.message_id)
        ticket = replace(ticket, supergroup_root_message_id=card.message_id)

        # Text is already rendered inside the card. Copy only non-text content;
        # otherwise the support chat gets the same text twice (card + reply).
        if message.content_type != "text":
            copied_id = await self._copy_to_support(message, ticket)
            if copied_id is None:
                await self._support_notice(
                    ticket,
                    "⚠️ Ticket created, but the first user message could not be copied. "
                    "Ask the user to resend it.",
                    user=user,
                )
                await self._send_user_message(
                    user.user_id,
                    f"Ticket #{ticket.id} is open, but I could not attach your first message. "
                    "Please send it again here.",
                )
            else:
                await self.db.add_ticket_message(
                    ticket_id=ticket.id,
                    chat_id=ticket.supergroup_chat_id,
                    message_id=copied_id,
                    direction="user_relay",
                    sender_user_id=user.user_id,
                    parent_message_id=card.message_id,
                )

        await self.db.touch_ticket(ticket.id, "user")
        ticket = await self._require_ticket(ticket.id)
        opened = await self._send_user_message(user.user_id, ticket_opened_text(ticket.id))
        if opened is not None:
            await self.db.set_ticket_intro(ticket.id, opened.message_id)
        return ticket

    async def _append_user_update(self, message: Message, ticket: Ticket, user: User) -> None:
        """Fold an open ticket's follow-up into the card instead of a new reply.

        Text is appended to the card and the card is edited in place. One
        attention notice is posted for the unanswered burst. Media cannot be
        appended to a text message, so an attachment is copied silently and the
        card gets a short marker.
        """
        if ticket.supergroup_root_message_id is None:
            await self._relay_user_content(message, ticket, user)
            return

        addition = self._update_line(message)
        await self.db.append_user_update(ticket.id, addition)
        await self._notify_user_update(await self._require_ticket(ticket.id))
        if message.content_type != "text":
            copied_id = await self._copy_to_support(message, ticket, silent=True, reply=False)
            if copied_id is not None:
                await self.db.add_ticket_message(
                    ticket_id=ticket.id,
                    chat_id=ticket.supergroup_chat_id,
                    message_id=copied_id,
                    direction="user_relay",
                    sender_user_id=user.user_id,
                    parent_message_id=ticket.supergroup_root_message_id,
                )
        await self.edit_card(await self._require_ticket(ticket.id))

    @staticmethod
    def _update_line(message: Message) -> str:
        timestamp = format_timestamp(utc_now())
        if message.content_type != "text":
            return f"\n\n💬 {timestamp}: 📎 [{message.content_type}]"
        body = truncate(message.text or "", 1000, preserve_newlines=True)
        return f"\n\n💬 {timestamp}: {body}"

    async def _notify_user_update(self, ticket: Ticket) -> None:
        """Send or refresh the single attention message for an unanswered burst."""
        if ticket.supergroup_root_message_id is None:
            return
        count = max(1, ticket.unanswered_updates)
        noun = "message" if count == 1 else "messages"
        text = (
            f"🔔 {html.bold(f'#{ticket.id}')} needs a reply · {count} new {noun}\n"
            "Reply to this notice (or the card) with /reply."
        )
        if ticket.notification_message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=ticket.supergroup_chat_id,
                    message_id=ticket.notification_message_id,
                    text=text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return
            except TelegramBadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                logger.info(
                    "could not update notification for ticket #%s: %s", ticket.id, exc
                )
                await self.db.set_notification_message(ticket.id, None)
            except TelegramAPIError:
                logger.exception("could not update notification for ticket #%s", ticket.id)
                return
        try:
            notice = await self.bot.send_message(
                chat_id=ticket.supergroup_chat_id,
                message_thread_id=ticket.supergroup_topic_id or None,
                text=text,
                reply_parameters=ReplyParameters(
                    message_id=ticket.supergroup_root_message_id,
                    allow_sending_without_reply=True,
                ),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError:
            logger.exception("could not send attention notification for ticket #%s", ticket.id)
            return
        await self.db.set_notification_message(ticket.id, notice.message_id)
        await self.db.add_ticket_message(
            ticket_id=ticket.id,
            chat_id=notice.chat.id,
            message_id=notice.message_id,
            direction="system",
            parent_message_id=ticket.supergroup_root_message_id,
        )

    async def _clear_attention(self, ticket: Ticket) -> None:
        """Remove the unanswered notification and clear the card warning."""
        message_id = ticket.notification_message_id
        if message_id is not None:
            try:
                await self.bot.delete_message(
                    chat_id=ticket.supergroup_chat_id,
                    message_id=message_id,
                )
            except TelegramBadRequest as exc:
                text = str(exc).lower()
                if "not found" not in text and "message to delete" not in text:
                    logger.info(
                        "could not delete notification for ticket #%s: %s",
                        ticket.id,
                        exc,
                    )
                # If deletion is not allowed, at least stop it looking actionable.
                try:
                    await self.bot.edit_message_text(
                        chat_id=ticket.supergroup_chat_id,
                        message_id=message_id,
                        text=f"✅ {html.bold(f'#{ticket.id}')} handled",
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                except TelegramAPIError:
                    logger.info("could not mark notification for ticket #%s handled", ticket.id)
            except TelegramAPIError:
                logger.warning("could not delete notification for ticket #%s", ticket.id)
        await self.db.clear_attention(ticket.id)

    async def _relay_user_content(self, message: Message, ticket: Ticket, user: User) -> None:
        if ticket.supergroup_root_message_id is None:
            raise SupportUnavailable(f"ticket #{ticket.id} has no card message")
        copied_id = await self._copy_to_support(message, ticket)
        if copied_id is None:
            await self._support_notice(
                ticket,
                f"⚠️ Could not relay a message from {esc(user.full_name)}. Ask them to resend.",
                user=user,
            )
            await self._send_user_message(
                user.user_id,
                f"I could not attach that message to ticket #{ticket.id}. Please resend it.",
            )
            return
        await self.db.add_ticket_message(
            ticket_id=ticket.id,
            chat_id=ticket.supergroup_chat_id,
            message_id=copied_id,
            direction="user_relay",
            sender_user_id=user.user_id,
            parent_message_id=ticket.supergroup_root_message_id,
        )
        await self.db.touch_ticket(ticket.id, "user")

    async def _copy_to_support(
        self,
        message: Message,
        ticket: Ticket,
        *,
        silent: bool = False,
        reply: bool = True,
    ) -> int | None:
        if ticket.supergroup_root_message_id is None:
            return None
        try:
            sent = await self.bot.copy_message(
                chat_id=ticket.supergroup_chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=ticket.supergroup_topic_id or None,
                disable_notification=silent,
                reply_parameters=ReplyParameters(
                    message_id=ticket.supergroup_root_message_id,
                    allow_sending_without_reply=True,
                ) if reply else None,
            )
        except TelegramAPIError:
            logger.exception("could not copy user message into ticket #%s", ticket.id)
            return None
        return sent.message_id

    # -- status ---------------------------------------------------------

    async def close(
        self,
        ticket: Ticket,
        *,
        by_user_id: int | None,
        reason: str | None = None,
        notify_user: bool = True,
        closing_text: str | None = None,
        by_admin: bool | None = None,
    ) -> Ticket:
        if by_admin is None:
            by_admin = by_user_id is not None and by_user_id != ticket.user_id
        await self._clear_attention(ticket)
        await self.db.close_ticket(ticket.id, by_user_id, reason)
        close_notice = ticket_closed_text(
            ticket.id, self.config.reopen_window_days
        )
        plain_close = ticket_closed_text(
            ticket.id,
            self.config.reopen_window_days,
            plain=True,
        )
        if closing_text:
            close_message = truncate(
                closing_text, 1000, preserve_newlines=True
            )
            if by_admin:
                card_close = f"🔒 Close message:\n👤 Admin: {close_message}"
            else:
                card_close = f"🔒 Close message: {close_message}"
        elif by_admin:
            card_close = admin_closed_card_text()
        elif by_user_id == ticket.user_id:
            card_close = "🔒 User closed the issue."
        else:
            card_close = f"🔒 Close message: {plain_close}"
        await self.db.append_system_update(
            ticket.id, f"\n\n{card_close}"
        )

        if notify_user:
            if by_admin:
                if closing_text:
                    body = (
                        f"{esc(truncate(closing_text, 3500, preserve_newlines=True))}"
                        f"\n\n——\n{close_notice}"
                    )
                else:
                    body = close_notice
                if closing_text:
                    dm_text = admin_text(body)
                else:
                    dm_text = admin_closed_without_message_text(
                        self.config.reopen_window_days
                    )
            else:
                dm_text = close_notice
        updated = await self._require_ticket(ticket.id)
        await self.edit_card(updated)
        if notify_user:
            await self._send_user_message(updated.user_id, dm_text)
        return updated

    async def reopen(self, ticket: Ticket, *, notify_user: bool = False) -> Ticket:
        await self._clear_attention(ticket)
        await self.db.reopen_ticket(ticket.id)
        updated = await self._require_ticket(ticket.id)
        await self.edit_card(updated)
        if notify_user:
            await self._send_user_message(
                updated.user_id, f"Ticket #{updated.id} reopened. Support will reply here."
            )
        return updated

    async def edit_card(self, ticket: Ticket | None) -> None:
        if ticket is None or ticket.supergroup_root_message_id is None:
            return
        user = await self.db.get_user(ticket.user_id)
        if user is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=ticket.supergroup_chat_id,
                message_id=ticket.supergroup_root_message_id,
                text=card_text(ticket, user),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramBadRequest as exc:
            # "message is not modified" is harmless; everything else is logged.
            if "not modified" not in str(exc).lower():
                logger.warning("could not edit card for ticket #%s: %s", ticket.id, exc)
        except TelegramAPIError:
            logger.exception("could not edit card for ticket #%s", ticket.id)

    # -- admin side -----------------------------------------------------

    async def send_admin_reply(
        self,
        message: Message,
        ticket: Ticket,
        text: str,
        *,
        sender_user_id: int | None,
    ) -> None:
        if not ticket.is_open:
            ticket = await self.reopen(ticket, notify_user=False)

        delivered = await self._send_plain_user_text(ticket, admin_text(text))
        if delivered is None:
            await self._delivery_failure(message, ticket, "the user cannot receive messages")
            return

        await self.db.add_ticket_message(
            ticket_id=ticket.id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            direction="admin_reply",
            sender_user_id=sender_user_id,
            parent_message_id=self._parent_id(message),
            mirror_chat_id=ticket.user_id,
            mirror_message_id=delivered.message_id,
        )
        await self.db.append_admin_update(
            ticket.id,
            f"\n\n👤 Admin: {truncate(text, 1000, preserve_newlines=True)}",
        )
        await self.db.touch_ticket(ticket.id, "admin")
        await self.db.clear_unreachable(ticket.id)
        await self._clear_attention(ticket)
        await self.edit_card(await self._require_ticket(ticket.id))
        await self._delete_message(message)

    async def _send_plain_user_text(self, ticket: Ticket, text: str) -> Message | None:
        try:
            return await self.bot.send_message(
                chat_id=ticket.user_id,
                text=text,
                parse_mode=None,
            )
        except TelegramForbiddenError as exc:
            await self.db.mark_unreachable(ticket.id, str(exc))
            logger.warning("user %s blocked the bot for ticket #%s", ticket.user_id, ticket.id)
            return None
        except TelegramAPIError:
            logger.exception("could not send a DM for ticket #%s", ticket.id)
            return None

    async def _delivery_failure(self, message: Message, ticket: Ticket, reason: str) -> None:
        await self.db.add_ticket_message(
            ticket_id=ticket.id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            direction="admin_reply",
            sender_user_id=message.from_user.id if message.from_user else None,
            parent_message_id=self._parent_id(message),
        )
        await self.db.mark_unreachable(ticket.id, reason)
        await self.edit_card(await self._require_ticket(ticket.id))
        user = await self.db.get_user(ticket.user_id)
        await self._reply_in_support(
            message,
            admin_delivery_error(user.username if user else None, reason),
        )

    async def _delete_message(self, message: Message) -> None:
        try:
            await self.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except TelegramBadRequest as exc:
            logger.info("could not delete admin message %s: %s", message.message_id, exc)
        except TelegramAPIError:
            logger.warning("could not delete admin message %s", message.message_id)

    async def close_from_reaction(self, update: MessageReactionUpdated) -> bool:
        if not any(
            isinstance(reaction, ReactionTypeEmoji) and reaction.emoji in CHECK_EMOJI
            for reaction in update.new_reaction
        ):
            return False
        if any(
            isinstance(reaction, ReactionTypeEmoji) and reaction.emoji in CHECK_EMOJI
            for reaction in update.old_reaction
        ):
            return False
        found = await self.db.find_ticket_by_message(update.chat.id, update.message_id)
        if found is None:
            return False
        ticket, _mapping = found
        if not ticket.is_open:
            return False
        await self.close(
            ticket,
            by_user_id=update.user.id if update.user else None,
            by_admin=True,
        )
        return True

    async def resolve_ticket_reply(self, message: Message) -> tuple[Ticket | None, str | None]:
        parent = message.reply_to_message
        if parent is None:
            return None, None
        found = await self.db.find_ticket_by_message(parent.chat.id, parent.message_id)
        if found is None:
            return None, None
        ticket, mapping = found
        return ticket, mapping.direction if mapping else None

    # -- shared helpers -------------------------------------------------

    async def _support_notice(
        self,
        ticket: Ticket,
        text: str,
        *,
        user: User | None = None,
    ) -> None:
        if ticket.supergroup_root_message_id is None:
            return
        try:
            notice = await self.bot.send_message(
                chat_id=ticket.supergroup_chat_id,
                message_thread_id=ticket.supergroup_topic_id or None,
                text=text,
                reply_parameters=ReplyParameters(
                    message_id=ticket.supergroup_root_message_id,
                    allow_sending_without_reply=True,
                ),
            )
            await self.db.add_ticket_message(
                ticket_id=ticket.id,
                chat_id=notice.chat.id,
                message_id=notice.message_id,
                direction="system",
                parent_message_id=ticket.supergroup_root_message_id,
            )
        except TelegramAPIError:
            logger.exception("could not send support notice for ticket #%s", ticket.id)

    async def _reply_in_support(self, message: Message, text: str) -> None:
        try:
            await message.reply(text)
        except TelegramAPIError:
            logger.exception("could not reply in the support chat")

    async def _send_user_message(self, user_id: int, text: str) -> Message | None:
        try:
            return await self.bot.send_message(chat_id=user_id, text=text)
        except TelegramAPIError as exc:
            logger.warning("could not send DM to %s: %s", user_id, exc)
            return None

    def _parent_id(self, message: Message) -> int | None:
        return message.reply_to_message.message_id if message.reply_to_message else None

    async def _require_ticket(self, ticket_id: int) -> Ticket:
        ticket = await self.db.get_ticket(ticket_id)
        if ticket is None:
            raise LookupError(f"ticket {ticket_id} disappeared")
        return ticket

    async def list_open(self) -> list[TicketWithUser]:
        return await self.db.list_open_tickets()

    def render_open_list(self, items: list[TicketWithUser], page: int) -> str:
        return open_list_text(items, page, 10, self.config)
