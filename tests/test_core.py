from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChatAdministrators,
)

from src.auth import AdminAuth
from src.admin_router import _command_argument, _too_long, _too_long_message, TELEGRAM_TEXT_LIMIT
from src.config import Config, ConfigError
from src.db import Database, Ticket, User
from src.texts import (
    admin_help_text,
    admin_text,
    card_text,
    open_list_text,
    ticket_closed_text,
)
from src.user_ticket_router import _new_argument, close_command
from src.tickets import TicketService
from main import ADMIN_COMMANDS, USER_COMMANDS, _set_commands
from src.utils import normalize_secret


def make_config(**overrides) -> Config:
    environ = {
        "BOT_TOKEN": "test-token",
        "SUPPORT_CHAT_ID": "-1001234567890",
        "TICKETS_TOPIC_ID": "42",
        "APP_ENV": "production",
        "SECRET_QUESTION": "City?",
        "SECRETS": "Madrid",
        "DB_PATH": "/tmp/beaver-test.db",
    }
    environ.update(overrides)
    return Config.from_env(environ=environ)


def make_ticket(**overrides) -> Ticket:
    base = dict(
        id=42,
        user_id=7,
        status="open",
        concern_preview="The invoice is wrong",
        origin_chat_id=7,
        origin_message_id=10,
        supergroup_chat_id=-1001234567890,
        supergroup_topic_id=42,
        supergroup_root_message_id=100,
        user_intro_message_id=11,
        opened_at="2026-11-08T10:00:00Z",
        last_activity_at="2026-11-08T10:00:00Z",
        last_activity_role="user",
        closed_at=None,
        closed_by_user_id=None,
        close_reason=None,
        reopened_count=0,
        unreachable_at=None,
        last_delivery_error=None,
        nudged_at=None,
    )
    base.update(overrides)
    return Ticket(**base)


def make_user(**overrides) -> User:
    base = dict(
        user_id=7,
        username="jane",
        full_name="Jane <Doe>",
        language_code="en",
        verified_at="2026-11-08T09:00:00Z",
        failed_attempts=0,
        cooldown_until=None,
        force_new_until=None,
        created_at="2026-11-08T08:00:00Z",
        last_seen_at="2026-11-08T10:00:00Z",
    )
    base.update(overrides)
    return User(**base)


class CommandMenuTests(unittest.TestCase):
    def test_scoped_command_menus(self):
        class FakeBot:
            def __init__(self):
                self.calls = []

            async def delete_my_commands(self):
                self.calls.append(("delete", None, None))

            async def set_my_commands(self, commands, scope=None):
                self.calls.append(("set", commands, scope))

        bot = FakeBot()
        config = make_config()
        asyncio.run(_set_commands(bot, config))

        self.assertEqual(bot.calls[0][0], "delete")
        self.assertIsInstance(bot.calls[1][2], BotCommandScopeAllPrivateChats)
        self.assertIn("start", [c.command for c in bot.calls[1][1]])
        self.assertNotIn("reply", [c.command for c in bot.calls[1][1]])
        self.assertIsInstance(bot.calls[2][2], BotCommandScopeAllGroupChats)
        self.assertEqual(bot.calls[2][1], [])
        self.assertIsInstance(bot.calls[3][2], BotCommandScopeChatAdministrators)
        admin_names = [c.command for c in bot.calls[3][1]]
        self.assertIn("reply", admin_names)
        self.assertIn("help", admin_names)
        self.assertNotIn("start", admin_names)
        self.assertNotIn("note", admin_names)

    def test_admin_help_mentions_explicit_commands(self):
        help_text = admin_help_text()
        self.assertIn("/reply", help_text)
        self.assertIn("/close", help_text)
        self.assertIn("internal", help_text)
        self.assertNotIn("/note", help_text)


class ConfigTests(unittest.TestCase):
    def test_normalize_secret(self):
        self.assertEqual(normalize_secret("  MADRID! "), "madrid")
        self.assertEqual(normalize_secret("New   York."), "new york")

    def test_staging_without_topic_uses_whole_chat(self):
        config = make_config(APP_ENV="staging", TICKETS_TOPIC_ID="0")
        self.assertFalse(config.uses_topic)
        self.assertEqual(config.support_chat_id, -1001234567890)

    def test_whole_chat_messages_are_also_accepted(self):
        config = make_config(TICKETS_TOPIC_ID="0")
        self.assertFalse(config.uses_topic)
        self.assertTrue(config.is_production)

    def test_forum_group_without_topic_links(self):
        config = make_config(TICKETS_TOPIC_ID="0")
        self.assertEqual(
            config.message_url(100, None), "https://t.me/c/1234567890/100"
        )

    def test_alias_conflict_fails(self):
        with self.assertRaises(ConfigError):
            make_config(SUPPORT_CHAT_ID="-1001234567890", SUPERGROUP_ID="-1009999999999")

    def test_negative_topic_fails(self):
        with self.assertRaises(ConfigError):
            make_config(TICKETS_TOPIC_ID="-1")

    def test_topic_url(self):
        config = make_config()
        self.assertEqual(
            config.message_url(100, 42),
            "https://t.me/c/1234567890/42/100",
        )


class CommandTests(unittest.TestCase):
    def test_reply_argument(self):
        self.assertEqual(_command_argument(SimpleNamespace(text="/reply hello there")), "hello there")
        self.assertEqual(_command_argument(SimpleNamespace(text="/reply@BeaverBot hi")), "hi")
        self.assertEqual(_command_argument(SimpleNamespace(text="/reply")), "")

    def test_close_argument(self):
        self.assertEqual(_command_argument(SimpleNamespace(text="/close done")), "done")
        self.assertEqual(_command_argument(SimpleNamespace(text="/close")), "")

    def test_new_argument(self):
        self.assertEqual(_new_argument(SimpleNamespace(text="/new my issue")), "my issue")
        self.assertEqual(_new_argument(SimpleNamespace(text="/new@BeaverBot my issue")), "my issue")
        self.assertEqual(_new_argument(SimpleNamespace(text="/new")), "")

    def test_text_limit(self):
        self.assertFalse(_too_long("x" * TELEGRAM_TEXT_LIMIT))
        self.assertTrue(_too_long("x" * (TELEGRAM_TEXT_LIMIT + 1)))
        self.assertIn(str(TELEGRAM_TEXT_LIMIT), _too_long_message(TELEGRAM_TEXT_LIMIT + 1))


class TextTests(unittest.TestCase):
    def test_card_escapes_user_html(self):
        text = card_text(make_ticket(), make_user())
        self.assertIn("&lt;Doe&gt;", text)
        self.assertNotIn("<Doe>", text)
        self.assertIn("#42", text)

    def test_admin_attribution(self):
        self.assertEqual(admin_text("hello"), "👤 Admin:\nhello")
        self.assertIn("has been marked as resolved", ticket_closed_text(42, 7))

    def test_card_fits_telegram_limit(self):
        ticket = make_ticket(concern_preview="<" * 5000, user_updates="&" * 5000)
        self.assertLess(len(card_text(ticket, make_user())), 4096)

    def test_update_line(self):
        text = SimpleNamespace(content_type="text", text="more details")
        self.assertIn("more details", TicketService._update_line(text))
        photo = SimpleNamespace(content_type="photo", text=None)
        self.assertIn("[photo]", TicketService._update_line(photo))

    def test_card_includes_user_updates(self):
        text = card_text(
            make_ticket(user_updates="\n\n💬 2026-11-08 10:01 UTC: more details"),
            make_user(),
        )
        self.assertIn("Updates", text)
        self.assertIn("more details", text)

    def test_card_preserves_update_newlines(self):
        text = card_text(
            make_ticket(
                user_updates=(
                    "\n\n💬 2026-11-08 10:01 UTC: first line\n"
                    "still first\n\n💬 2026-11-08 10:02 UTC: second"
                )
            ),
            make_user(),
        )
        self.assertIn("first line\nstill first\n\n💬 2026-11-08 10:02 UTC: second", text)

    def test_card_shows_needs_reply_banner(self):
        text = card_text(make_ticket(unanswered_updates=2), make_user())
        self.assertIn("NEEDS REPLY", text)
        self.assertIn("2 new messages", text)
        closed = card_text(
            make_ticket(status="closed", unanswered_updates=2), make_user()
        )
        self.assertNotIn("NEEDS REPLY", closed)

    def test_card_hides_banner_when_answered(self):
        text = card_text(make_ticket(unanswered_updates=0), make_user())
        self.assertNotIn("NEEDS REPLY", text)

    def test_open_list_groups_queues(self):
        config = make_config()
        waiting_support = SimpleNamespace(
            ticket=make_ticket(id=1, last_activity_role="user"),
            full_name="Jane",
            username="jane",
        )
        waiting_user = SimpleNamespace(
            ticket=make_ticket(id=2, last_activity_role="admin"),
            full_name="Sam",
            username=None,
        )
        text = open_list_text([waiting_support, waiting_user], 1, 10, config)
        self.assertIn("Waiting for support", text)
        self.assertIn("Waiting for user", text)
        self.assertIn("#1", text)
        self.assertIn("#2", text)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_ticket_lifecycle(self):
        user = await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        self.assertFalse(user.is_verified)
        await self.db.set_verified(7)
        user = await self.db.get_user(7)
        self.assertTrue(user.is_verified)

        ticket_id = await self.db.create_ticket(
            7, "hello", 7, 10, -1001234567890, 42
        )
        await self.db.set_ticket_root(ticket_id, 100)
        ticket = await self.db.get_ticket(ticket_id)
        self.assertEqual(ticket.status, "open")
        self.assertEqual(ticket.supergroup_topic_id, 42)

        await self.db.add_ticket_message(
            ticket_id, -1001234567890, 101, "user_relay", 7, 100
        )
        found = await self.db.find_ticket_by_message(-1001234567890, 101)
        self.assertIsNotNone(found)
        self.assertEqual(found[0].id, ticket_id)
        self.assertEqual(found[1].direction, "user_relay")

        await self.db.close_ticket(ticket_id, 1, "done")
        self.assertIsNone(await self.db.get_open_ticket_for_user(7))
        await self.db.reopen_ticket(ticket_id)
        self.assertIsNotNone(await self.db.get_open_ticket_for_user(7))

    async def test_one_open_ticket_per_user(self):
        await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        first = await self.db.create_ticket(7, "one", 7, 1, -1001234567890, 42)
        with self.assertRaises(Exception):
            await self.db.create_ticket(7, "two", 7, 2, -1001234567890, 42)
        await self.db.close_ticket(first, None)
        second = await self.db.create_ticket(7, "three", 7, 3, -1001234567890, 42)
        self.assertGreater(second, first)

    async def test_append_user_update(self):
        await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await self.db.create_ticket(7, "hi", 7, 1, -1001234567890, 0)
        await self.db.append_user_update(ticket_id, "\n\nmore")
        ticket = await self.db.get_ticket(ticket_id)
        self.assertEqual(ticket.user_updates, "\n\nmore")
        self.assertEqual(ticket.last_activity_role, "user")
        self.assertEqual(ticket.unanswered_updates, 1)
        self.assertIsNotNone(ticket.awaiting_admin_since)

        await self.db.append_user_update(ticket_id, "\n\nagain")
        ticket = await self.db.get_ticket(ticket_id)
        self.assertEqual(ticket.unanswered_updates, 2)

        await self.db.set_notification_message(ticket_id, 900)
        self.assertEqual((await self.db.get_ticket(ticket_id)).notification_message_id, 900)
        await self.db.clear_attention(ticket_id)
        ticket = await self.db.get_ticket(ticket_id)
        self.assertEqual(ticket.unanswered_updates, 0)
        self.assertIsNone(ticket.awaiting_admin_since)
        self.assertIsNone(ticket.notification_message_id)

    async def test_append_admin_update(self):
        await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await self.db.create_ticket(7, "hi", 7, 1, -1001234567890, 0)
        await self.db.append_admin_update(ticket_id, "\n\n👤 Admin: hello")
        ticket = await self.db.get_ticket(ticket_id)
        self.assertIn("Admin: hello", ticket.user_updates)
        self.assertEqual(ticket.last_activity_role, "admin")

    async def test_append_system_update(self):
        await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await self.db.create_ticket(7, "hi", 7, 1, -1001234567890, 0)
        await self.db.append_system_update(ticket_id, "\n\n🔒 Close message: done")
        ticket = await self.db.get_ticket(ticket_id)
        self.assertIn("Close message: done", ticket.user_updates)
        self.assertEqual(ticket.last_activity_role, "system")

    async def test_whole_chat_ticket_roundtrip(self):
        await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await self.db.create_ticket(7, "hi", 7, 1, -1001234567890, 0)
        ticket = await self.db.get_ticket(ticket_id)
        # Stored 0 means "whole chat" and reads back as no topic.
        self.assertIsNone(ticket.supergroup_topic_id)

    async def test_clear_tickets(self):
        await self.db.upsert_user(7, "jane", "Jane Doe", "en")
        await self.db.set_force_new(7, "2099-01-01T00:00:00Z")
        ticket_id = await self.db.create_ticket(7, "hi", 7, 1, -1001234567890, 0)
        await self.db.add_ticket_message(
            ticket_id, -1001234567890, 101, "user_relay", 7, None
        )
        removed = await self.db.clear_tickets()
        self.assertEqual(removed, (1, 1))
        self.assertIsNone(await self.db.get_ticket(ticket_id))
        self.assertIsNone(await self.db.find_ticket_by_message(-1001234567890, 101))
        user = await self.db.get_user(7)
        self.assertIsNone(user.force_new_until)
        # The ticket sequence is reset so a fresh staging run starts at #1.
        self.assertEqual(
            await self.db.create_ticket(7, "again", 7, 2, -1001234567890, 0),
            1,
        )


class TicketCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_ticket_does_not_copy_first_message(self):
        config = make_config()
        user = make_user()
        ticket = make_ticket(id=1, concern_preview="hello", supergroup_root_message_id=None)

        class FakeBot:
            def __init__(self):
                self.support_messages = []
                self.copy_calls = 0

            async def send_message(self, **kwargs):
                if kwargs["chat_id"] == config.support_chat_id:
                    self.support_messages.append(kwargs)
                    return SimpleNamespace(message_id=100)
                return SimpleNamespace(message_id=200)

            async def copy_message(self, **kwargs):
                self.copy_calls += 1
                raise AssertionError("text tickets must not copy the first message")

        class FakeDb:
            def __init__(self):
                self.ticket = ticket

            async def create_ticket(self, *args, **kwargs):
                return 1

            async def get_ticket(self, ticket_id):
                return self.ticket

            async def set_ticket_root(self, ticket_id, message_id):
                self.ticket = replace(self.ticket, supergroup_root_message_id=message_id)

            async def touch_ticket(self, ticket_id, role):
                pass

            async def set_ticket_intro(self, ticket_id, message_id):
                pass

            async def add_ticket_message(self, **kwargs):
                raise AssertionError("text tickets must not add a relay mapping")

        bot = FakeBot()
        app = SimpleNamespace(bot=bot, config=config, db=FakeDb(), delivery_thread_id=42)
        message = SimpleNamespace(
            content_type="text",
            text="hello",
            caption=None,
            chat=SimpleNamespace(id=7),
            message_id=10,
            reply_to_message=None,
        )
        await TicketService(app)._create_ticket(message, user)
        self.assertEqual(bot.copy_calls, 0)
        self.assertEqual(len(bot.support_messages), 1)
        self.assertIn("hello", bot.support_messages[0]["text"])

    async def test_inline_new_uses_issue_text(self):
        config = make_config()
        user = make_user()
        ticket = make_ticket(id=2, concern_preview="old", supergroup_root_message_id=None)

        class FakeBot:
            def __init__(self):
                self.support_messages = []

            async def send_message(self, **kwargs):
                if kwargs["chat_id"] == config.support_chat_id:
                    self.support_messages.append(kwargs)
                    return SimpleNamespace(message_id=300)
                return SimpleNamespace(message_id=301)

            async def copy_message(self, **kwargs):
                raise AssertionError("inline /new text must not be copied")

        class FakeDb:
            def __init__(self):
                self.ticket = ticket

            async def create_ticket(self, *args, **kwargs):
                self.preview = kwargs.get("preview")
                self.ticket = replace(self.ticket, concern_preview=self.preview)
                return 2

            async def get_ticket(self, ticket_id):
                return self.ticket

            async def set_ticket_root(self, ticket_id, message_id):
                self.ticket = replace(self.ticket, supergroup_root_message_id=message_id)

            async def touch_ticket(self, ticket_id, role):
                pass

            async def set_ticket_intro(self, ticket_id, message_id):
                pass

            async def add_ticket_message(self, **kwargs):
                pass

            async def set_force_new(self, user_id, value):
                pass

        bot = FakeBot()
        db = FakeDb()
        app = SimpleNamespace(bot=bot, config=config, db=db, delivery_thread_id=42)
        message = SimpleNamespace(
            content_type="text",
            text="/new my fresh issue",
            caption=None,
            chat=SimpleNamespace(id=7),
            message_id=20,
            reply_to_message=None,
        )
        await TicketService(app).create_new(message, user, "my fresh issue")
        self.assertEqual(db.preview, "my fresh issue")
        self.assertIn("my fresh issue", bot.support_messages[0]["text"])
        self.assertNotIn("/new", bot.support_messages[0]["text"])


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_created_edited_and_cleared(self):
        config = make_config()

        class FakeBot:
            def __init__(self):
                self.sent = []
                self.edits = []
                self.deleted = []

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return SimpleNamespace(
                    message_id=900, chat=SimpleNamespace(id=kwargs["chat_id"])
                )

            async def edit_message_text(self, **kwargs):
                self.edits.append(kwargs)

            async def delete_message(self, **kwargs):
                self.deleted.append(kwargs)

        class FakeDb:
            def __init__(self):
                self.notification = None
                self.mapping = None
                self.cleared = False

            async def set_notification_message(self, ticket_id, message_id):
                self.notification = message_id

            async def add_ticket_message(self, **kwargs):
                self.mapping = kwargs

            async def clear_attention(self, ticket_id):
                self.cleared = True

        bot = FakeBot()
        db = FakeDb()
        app = SimpleNamespace(bot=bot, config=config, db=db, delivery_thread_id=42)
        service = TicketService(app)

        ticket = make_ticket(unanswered_updates=2)
        await service._notify_user_update(ticket)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("needs a reply", bot.sent[0]["text"])
        self.assertIn("2 new messages", bot.sent[0]["text"])
        self.assertEqual(db.notification, 900)
        self.assertEqual(db.mapping["direction"], "system")

        await service._notify_user_update(replace(ticket, notification_message_id=900))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0]["message_id"], 900)

        await service._clear_attention(replace(ticket, notification_message_id=900))
        self.assertEqual(bot.deleted[0]["message_id"], 900)
        self.assertTrue(db.cleared)


class CloseMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_close_without_message_uses_short_card_text(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "close.db")
        await db.connect()
        await db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await db.create_ticket(7, "hi", 7, 1, -1001234567890, 42)
        await db.set_ticket_root(ticket_id, 100)
        ticket = await db.get_ticket(ticket_id)

        class FakeBot:
            def __init__(self):
                self.card = None

            async def edit_message_text(self, **kwargs):
                self.card = kwargs["text"]

        bot = FakeBot()
        app = SimpleNamespace(
            bot=bot, db=db, config=make_config(), delivery_thread_id=42
        )
        updated = await TicketService(app).close(
            ticket, by_user_id=1, notify_user=False
        )
        self.assertIn("🔒 Closed by an admin.", updated.user_updates)
        self.assertNotIn("🔒 Close message:", updated.user_updates)
        self.assertIn("🔒 Closed by an admin.", bot.card)
        await db.close()
        tmp.cleanup()

    async def test_admin_close_without_message_dm_text(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "close-dm.db")
        await db.connect()
        await db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await db.create_ticket(7, "hi", 7, 1, -1001234567890, 42)
        await db.set_ticket_root(ticket_id, 100)
        ticket = await db.get_ticket(ticket_id)

        class FakeBot:
            def __init__(self):
                self.card = None
                self.sent = []

            async def edit_message_text(self, **kwargs):
                self.card = kwargs["text"]

            async def send_message(self, **kwargs):
                self.sent.append(kwargs["text"])

        bot = FakeBot()
        app = SimpleNamespace(
            bot=bot, db=db, config=make_config(), delivery_thread_id=42
        )
        await TicketService(app).close(
            ticket, by_user_id=1, notify_user=True, by_admin=True
        )
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(
            bot.sent[-1],
            "👤 This issue has been closed by an admin without a message. "
            "Sorry! If this isn't resolved, reply here within 7 days to reopen "
            "it, or use /new for a different issue.",
        )
        await db.close()
        tmp.cleanup()

    async def test_admin_close_same_user_is_not_user_close(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "close-same-user.db")
        await db.connect()
        await db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await db.create_ticket(7, "hi", 7, 1, -1001234567890, 42)
        await db.set_ticket_root(ticket_id, 100)
        ticket = await db.get_ticket(ticket_id)

        class FakeBot:
            def __init__(self):
                self.card = None

            async def edit_message_text(self, **kwargs):
                self.card = kwargs["text"]

        bot = FakeBot()
        app = SimpleNamespace(
            bot=bot, db=db, config=make_config(), delivery_thread_id=42
        )
        updated = await TicketService(app).close(
            ticket, by_user_id=7, notify_user=False, by_admin=True
        )
        self.assertIn("🔒 Closed by an admin.", updated.user_updates)
        self.assertNotIn("has been marked as resolved", updated.user_updates)
        self.assertNotIn("🔒 User closed the issue.", updated.user_updates)
        self.assertNotIn("🔒 User closed the issue.", bot.card)
        await db.close()
        tmp.cleanup()

    async def test_close_with_text_is_single_dm_and_actual_card_message(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "close-text.db")
        await db.connect()
        await db.upsert_user(7, "jane", "Jane Doe", "en")
        ticket_id = await db.create_ticket(7, "hi", 7, 1, -1001234567890, 42)
        await db.set_ticket_root(ticket_id, 100)
        ticket = await db.get_ticket(ticket_id)

        class FakeBot:
            def __init__(self):
                self.card = None
                self.sent = []

            async def edit_message_text(self, **kwargs):
                self.card = kwargs["text"]

            async def send_message(self, **kwargs):
                self.sent.append(kwargs["text"])

        bot = FakeBot()
        app = SimpleNamespace(
            bot=bot, db=db, config=make_config(), delivery_thread_id=42
        )
        await TicketService(app).close(
            ticket, by_user_id=1, notify_user=True, closing_text="Restart it"
        )
        self.assertEqual(len(bot.sent), 1)
        self.assertTrue(bot.sent[0].startswith("👤 Admin:\nRestart it\n\n——\n"))
        self.assertIn("has been marked as resolved", bot.sent[0])
        self.assertIn("🔒 Close message:\n👤 Admin: Restart it", bot.card)
        self.assertNotIn("Ticket #" + str(ticket_id) + " has been marked", bot.card)
        await db.close()
        tmp.cleanup()


class UserCloseCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_close_updates_card_without_dm_notice(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "user-close.db")
        await db.connect()
        await db.upsert_user(7, "jane", "Jane Doe", "en")
        await db.set_verified(7)
        ticket_id = await db.create_ticket(7, "hi", 7, 1, -1001234567890, 42)
        await db.set_ticket_root(ticket_id, 100)

        class FakeBot:
            def __init__(self):
                self.card = None
                self.sent = []

            async def edit_message_text(self, **kwargs):
                self.card = kwargs["text"]

            async def send_message(self, **kwargs):
                self.sent.append(kwargs["text"])

        bot = FakeBot()
        app = SimpleNamespace(
            bot=bot, db=db, config=make_config(), delivery_thread_id=42
        )
        answers = []

        class FakeMessage:
            from_user = SimpleNamespace(
                id=7, username="jane", full_name="Jane Doe", language_code="en"
            )
            chat = SimpleNamespace(type="private")
            text = "/close"

            async def answer(self, text):
                answers.append(text)

        await close_command(FakeMessage(), app)
        self.assertEqual((await db.get_ticket(ticket_id)).status, "closed")
        self.assertIn("🔒 User closed the issue.", bot.card)
        self.assertNotIn("🔒 Close message:", bot.card)
        self.assertEqual(len(answers), 1)
        self.assertIn("has been marked as resolved", answers[0])
        self.assertEqual(bot.sent, [])
        await db.close()
        tmp.cleanup()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_database_gets_notification_columns(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "v2.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, user_updates TEXT NOT NULL DEFAULT '')")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        db = Database(path)
        await db.connect()
        async with db.conn.execute("PRAGMA user_version") as cursor:
            self.assertEqual((await cursor.fetchone())[0], 3)
        async with db.conn.execute("PRAGMA table_info(tickets)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        self.assertIn("awaiting_admin_since", columns)
        self.assertIn("unanswered_updates", columns)
        self.assertIn("notification_message_id", columns)
        await db.close()
        tmp.cleanup()


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_and_cached_admins(self):
        class FakeBot:
            async def get_chat_administrators(self, chat_id):
                return [SimpleNamespace(user=SimpleNamespace(id=2))]

        config = make_config(ADMIN_IDS="1")
        auth = AdminAuth(FakeBot(), config)
        self.assertTrue(await auth.is_admin_user(1))
        self.assertTrue(await auth.is_admin_user(2))
        self.assertFalse(await auth.is_admin_user(3))


if __name__ == "__main__":
    unittest.main()
