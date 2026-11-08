from __future__ import annotations

from aiogram import html
from aiogram.types import Chat, Message

from src.config import Config
from src.db import Ticket, TicketWithUser, User
from src.utils import format_timestamp, human_age, truncate


def esc(value: str | None) -> str:
    return html.quote(value or "")


def _escaped_excerpt(
    value: str, limit: int, *, preserve_newlines: bool = False
) -> str:
    """Return escaped text whose rendered HTML stays within ``limit`` chars."""
    value = truncate(value, limit, preserve_newlines=preserve_newlines)
    escaped = esc(value)
    if len(escaped) <= limit:
        return escaped
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if len(esc(value[:mid])) <= limit:
            low = mid
        else:
            high = mid - 1
    return esc(value[:low].rstrip()) + "…"


def _escaped_tail(value: str, limit: int) -> str:
    """Like _escaped_excerpt, but keep the END of the text.

    Used for the card's update log: when it overflows, the newest entries are
    the ones an admin needs, so older ones are dropped from the front. The cut
    snaps to the next entry boundary (blank line) so no entry starts mid-way.
    """
    value = truncate(value, len(value) + 1, preserve_newlines=True)
    escaped = esc(value)
    if len(escaped) <= limit:
        return escaped
    marker = "…\n"
    budget = limit - len(marker)
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if len(esc(value[len(value) - mid :])) <= budget:
            low = mid
        else:
            high = mid - 1
    tail = value[len(value) - low :]
    boundary = tail.find("\n\n")
    if boundary != -1 and boundary + 2 < len(tail):
        tail = tail[boundary + 2 :]
    return marker + esc(tail.lstrip())


def challenge_text(question: str) -> str:
    return (
        "Hi! To keep spam out of support, please answer one question:\n\n"
        f"{html.bold(esc(question))}"
    )


def wrong_secret_text(remaining: int) -> str:
    if remaining <= 0:
        return "That's not it. Please wait a while before trying again."
    return f"That's not it. Try again ({remaining} attempt{'s' if remaining != 1 else ''} left)."


def verified_text() -> str:
    return "That's right ✅ What do you need help with? Send a message, photo, or file."


def status_text(ticket: Ticket | None) -> str:
    if ticket is None:
        return "You have no open ticket. Send a message to start one."
    lines = [
        f"🎫 {html.bold(f'#{ticket.id}')} · open",
        f"Opened {format_timestamp(ticket.opened_at)}",
        f"Last update {human_age(ticket.last_activity_at)} ago",
    ]
    if ticket.reopened_count:
        lines.append(f"Reopened {ticket.reopened_count} time{'s' if ticket.reopened_count != 1 else ''}")
    lines.append(
        "Just send another message here to add details, or /close when it is resolved."
    )
    return "\n".join(lines)


def ticket_opened_text(ticket_id: int) -> str:
    return (
        f"✅ Ticket {html.bold(f'#{ticket_id}')} is open. Support can reply here in this chat.\n"
        "Have more details? Just send another message here. Use /close when it is resolved."
    )


def admin_text(text: str) -> str:
    """Prefix text delivered to a user so it is clearly from a human admin."""
    return f"👤 Admin:\n{text}"


def admin_closed_card_text() -> str:
    return "🔒 Closed by an admin."


def admin_closed_without_message_text(reopen_window_days: int) -> str:
    return (
        "👤 This issue has been closed by an admin without a message. Sorry! "
        f"If this isn't resolved, reply here within {reopen_window_days} days "
        "to reopen it, or use /new for a different issue."
    )


def ticket_closed_text(
    ticket_id: int,
    reopened_window_days: int,
    *,
    plain: bool = False,
) -> str:
    reference = f"#{ticket_id}" if plain else html.bold(f"#{ticket_id}")
    return (
        f"Ticket {reference} has been marked as resolved.\n"
        f"If it is not actually fixed, reply here within {reopened_window_days} days "
        "to reopen it, or use /new for a different issue."
    )



def admin_help_text() -> str:
    return (
        f"{html.bold('Admin guide')}\n"
        "Reply to a ticket card or notice with /reply &lt;text&gt;. The user gets a DM and the\n"
        "text is folded into the card; your command is then deleted from this chat.\n"
        "Use /close [text] to resolve it, with an optional final message.\n"
        "A plain reply in this chat is internal team chat and is never sent.\n"
        "Use /open to see waiting tickets, /reopen to reopen, /whereami for IDs.\n"
        "When a user adds a follow-up, the card shows 🔴 NEEDS REPLY and one notice\n"
        "is posted or refreshed. It is removed after /reply or /close.\n"
        "Mistyped commands are DM'd back to you and removed from this chat.\n"
        "React ✅ to close without a message."
    )

def card_text(ticket: Ticket, user: User) -> str:
    if ticket.status == "closed":
        status = "closed ✅"
    elif ticket.unreachable_at:
        status = "open · ⚠️ user unreachable"
    elif ticket.reopened_count:
        status = "open · reopened"
    else:
        status = "open"

    username = f" (@{esc(user.username)})" if user.username else ""
    name_line = f"{esc(user.full_name)}{username} · {user.user_id}"
    lines: list[str] = []
    if ticket.is_open and ticket.unanswered_updates:
        count = ticket.unanswered_updates
        noun = "message" if count == 1 else "messages"
        lines.append(f"🔴 {html.bold('NEEDS REPLY')} · {count} new {noun}")
    lines.extend(
        [
            f"🦦 {html.bold(f'#{ticket.id}')} · {status}",
            name_line,
            f"{format_timestamp(ticket.opened_at)} · last update {human_age(ticket.last_activity_at)} ago",
        ]
    )
    if ticket.concern_preview:
        # The card is the ticket's first message for text, so show the body
        # here rather than sending a second, duplicate relay.
        lines.append(f"“{_escaped_excerpt(ticket.concern_preview, 1800, preserve_newlines=True)}”")
    if ticket.user_updates.strip():
        lines.append("")
        lines.append(html.bold("Updates"))
        lines.append(_escaped_tail(ticket.user_updates.strip(), 1600))
    if ticket.unreachable_at:
        lines.append("⚠️ The bot cannot DM this user. Keep the username visible for manual contact.")
    return "\n".join(lines)


def open_list_text(
    items: list[TicketWithUser],
    page: int,
    page_size: int,
    config: Config,
) -> str:
    if not items:
        return "No open tickets. 🎉"

    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]

    support = [item for item in page_items if item.ticket.last_activity_role != "admin"]
    waiting = [item for item in page_items if item.ticket.last_activity_role == "admin"]

    def line(index: int, item: TicketWithUser) -> str:
        ticket = item.ticket
        who = esc(item.full_name)
        if item.username:
            who += f" (@{esc(item.username)})"
        preview = esc(truncate(ticket.concern_preview or "[no preview]", 60))
        link = config.message_url(
            ticket.supergroup_root_message_id or 0, ticket.supergroup_topic_id
        )
        return (
            f"{index}. #{ticket.id} · {human_age(ticket.last_activity_at)} · {who} · "
            f"“{preview}” · {html.link('open', link)}"
        )

    lines: list[str] = []
    index = start + 1
    if support:
        lines.append(html.bold("Waiting for support (oldest first)"))
        for item in support:
            lines.append(line(index, item))
            index += 1
    if waiting:
        lines.append("")
        lines.append(html.bold("Waiting for user"))
        for item in waiting:
            lines.append(line(index, item))
            index += 1
    lines.append("")
    lines.append(f"Page {page}/{total_pages} · use /open {page + 1} for the next page")
    return "\n".join(lines)


def whereami_text(
    config: Config,
    *,
    chat: Chat,
    is_support_forum: bool | None,
    thread_id: int | None,
    bot_is_admin: bool,
    sender_is_admin: bool,
) -> str:
    lines = [
        html.bold("Where am I?"),
        f"APP_ENV: {esc(config.app_env)}",
        f"Support chat: {config.support_chat_id}",
        f"Configured topic: {config.tickets_topic_id or 'whole chat (no topic)'}",
        f"This chat: {chat.id} ({esc(chat.type)})",
        f"Support group is a forum: {is_support_forum}",
        f"This message thread: {thread_id if thread_id is not None else 'General / DM'}",
        f"Bot is admin: {'yes' if bot_is_admin else 'no'}",
        f"You are admin: {'yes' if sender_is_admin else 'no'}",
    ]
    if not is_support_forum:
        lines.append("")
        lines.append(
            "Plain group: every ticket goes into the chat itself. No topic needed."
        )
    elif not config.uses_topic:
        lines.append("")
        lines.append(
            "Forum without TICKETS_TOPIC_ID: tickets go to General. Set a topic id "
            "here if you want them in their own topic."
        )
    return "\n".join(lines)


def preview_for_message(message: Message) -> str:
    # Keep the full first text: the card renders it and no separate copy is
    # sent for text tickets. `/open` truncates the preview independently.
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return f"[{message.content_type}]"


def admin_delivery_error(username: str | None, reason: str) -> str:
    who = f"@{esc(username)}" if username else "the user"
    return f"⚠️ Could not DM {who}: {esc(reason)}. The reply was not delivered."


def unknown_command_text() -> str:
    return "Unknown command. Use /help to see what I can do."
