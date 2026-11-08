from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import aiosqlite

from src.utils import utc_now

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id              INTEGER PRIMARY KEY,
    username             TEXT,
    full_name            TEXT NOT NULL,
    language_code        TEXT,
    verified_at          TEXT,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    cooldown_until       TEXT,
    force_new_until      TEXT,
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                   INTEGER NOT NULL REFERENCES users(user_id),
    status                    TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open', 'closed')),
    concern_preview           TEXT,
    origin_chat_id            INTEGER NOT NULL,
    origin_message_id         INTEGER NOT NULL,
    supergroup_chat_id        INTEGER NOT NULL,
    supergroup_topic_id       INTEGER NOT NULL DEFAULT 0,  -- 0 = whole chat
    supergroup_root_message_id INTEGER,
    user_intro_message_id     INTEGER,
    opened_at                 TEXT NOT NULL,
    last_activity_at          TEXT NOT NULL,
    last_activity_role        TEXT NOT NULL DEFAULT 'user'
                              CHECK (last_activity_role IN ('user','admin','system')),
    closed_at                 TEXT,
    closed_by_user_id         INTEGER,
    close_reason              TEXT,
    reopened_count            INTEGER NOT NULL DEFAULT 0,
    unreachable_at            TEXT,
    last_delivery_error       TEXT,
    nudged_at                 TEXT,
    user_updates              TEXT NOT NULL DEFAULT '',
    awaiting_admin_since      TEXT,
    unanswered_updates        INTEGER NOT NULL DEFAULT 0,
    notification_message_id   INTEGER,
    UNIQUE (supergroup_chat_id, supergroup_root_message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_user
    ON tickets(user_id) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS tickets_open_order
    ON tickets(status, last_activity_at);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    chat_id         INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN (
                        'user_relay',
                        'admin_reply',
                        'admin_internal',
                        'system'
                    )),
    sender_user_id  INTEGER,
    parent_message_id INTEGER,
    mirror_chat_id  INTEGER,
    mirror_message_id INTEGER,
    created_at      TEXT NOT NULL,
    edited_at       TEXT,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ticket_messages_by_ticket
    ON ticket_messages(ticket_id);

CREATE UNIQUE INDEX IF NOT EXISTS ticket_messages_by_mirror
    ON ticket_messages(mirror_chat_id, mirror_message_id)
    WHERE mirror_message_id IS NOT NULL;
"""


class DatabaseError(RuntimeError):
    pass


def _from_row(cls: type, row: aiosqlite.Row):
    values = {field.name: row[field.name] for field in fields(cls)}
    if cls is Ticket and not values.get("supergroup_topic_id"):
        # Stored 0 means "the whole chat"; expose it as no topic.
        values["supergroup_topic_id"] = None
    return cls(**values)


@dataclass(frozen=True)
class User:
    user_id: int
    username: str | None
    full_name: str
    language_code: str | None
    verified_at: str | None
    failed_attempts: int
    cooldown_until: str | None
    force_new_until: str | None
    created_at: str
    last_seen_at: str

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None


@dataclass(frozen=True)
class Ticket:
    id: int
    user_id: int
    status: str
    concern_preview: str | None
    origin_chat_id: int
    origin_message_id: int
    supergroup_chat_id: int
    supergroup_topic_id: int | None  # None = whole chat
    supergroup_root_message_id: int | None
    user_intro_message_id: int | None
    opened_at: str
    last_activity_at: str
    last_activity_role: str
    closed_at: str | None
    closed_by_user_id: int | None
    close_reason: str | None
    reopened_count: int
    unreachable_at: str | None
    last_delivery_error: str | None
    nudged_at: str | None
    user_updates: str = ""
    awaiting_admin_since: str | None = None
    unanswered_updates: int = 0
    notification_message_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(frozen=True)
class TicketMessage:
    id: int
    ticket_id: int
    chat_id: int
    message_id: int
    direction: str
    sender_user_id: int | None
    parent_message_id: int | None
    mirror_chat_id: int | None
    mirror_message_id: int | None
    created_at: str
    edited_at: str | None


@dataclass(frozen=True)
class TicketWithUser:
    ticket: Ticket
    full_name: str
    username: str | None


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        await self._migrate()
        logger.info("database ready: %s", self.path)

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def _migrate(self) -> None:
        assert self.conn is not None
        async with self.conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
            version = int(row[0]) if row else 0
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
            )
        if version < 1:
            await self.conn.executescript(SCHEMA)
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("database migrated to schema version %s", SCHEMA_VERSION)
            version = SCHEMA_VERSION
        if version == 1:
            await self._add_column(
                "tickets", "user_updates TEXT NOT NULL DEFAULT ''"
            )
            version = 2
        if version == 2:
            await self._add_column("tickets", "awaiting_admin_since TEXT")
            await self._add_column(
                "tickets", "unanswered_updates INTEGER NOT NULL DEFAULT 0"
            )
            await self._add_column("tickets", "notification_message_id INTEGER")
            version = 3
        if version != SCHEMA_VERSION:
            raise DatabaseError(f"unsupported database schema version {version}")
        if version == SCHEMA_VERSION:
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()

    async def _add_column(self, table: str, definition: str) -> None:
        assert self.conn is not None
        try:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    async def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        assert self.conn is not None
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        assert self.conn is not None
        async with self.conn.execute(query, params) as cursor:
            return list(await cursor.fetchall())

    async def _execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(query, params)
        await self.conn.commit()
        return cursor.lastrowid

    # -- users ---------------------------------------------------------

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        language_code: str | None,
    ) -> User:
        now = utc_now()
        await self._execute(
            """
            INSERT INTO users (user_id, username, full_name, language_code, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                language_code = excluded.language_code,
                last_seen_at = excluded.last_seen_at
            """,
            (user_id, username, full_name, language_code, now, now),
        )
        user = await self.get_user(user_id)
        assert user is not None
        return user

    async def get_user(self, user_id: int) -> User | None:
        row = await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return _from_row(User, row) if row else None

    async def set_verified(self, user_id: int) -> None:
        await self._execute(
            """
            UPDATE users
            SET verified_at = COALESCE(verified_at, ?), failed_attempts = 0,
                cooldown_until = NULL, last_seen_at = ?
            WHERE user_id = ?
            """,
            (utc_now(), utc_now(), user_id),
        )

    async def set_failed_attempts(
        self, user_id: int, attempts: int, cooldown_until: str | None
    ) -> None:
        await self._execute(
            "UPDATE users SET failed_attempts = ?, cooldown_until = ? WHERE user_id = ?",
            (attempts, cooldown_until, user_id),
        )

    async def set_force_new(self, user_id: int, until: str | None) -> None:
        await self._execute(
            "UPDATE users SET force_new_until = ? WHERE user_id = ?", (until, user_id)
        )

    # -- tickets -------------------------------------------------------

    async def create_ticket(
        self,
        user_id: int,
        preview: str,
        origin_chat_id: int,
        origin_message_id: int,
        support_chat_id: int,
        topic_id: int,
    ) -> int:
        now = utc_now()
        ticket_id = await self._execute(
            """
            INSERT INTO tickets (
                user_id, status, concern_preview, origin_chat_id, origin_message_id,
                supergroup_chat_id, supergroup_topic_id, opened_at,
                last_activity_at, last_activity_role
            ) VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, 'user')
            """,
            (user_id, preview, origin_chat_id, origin_message_id, support_chat_id, topic_id, now, now),
        )
        return int(ticket_id)

    async def set_ticket_root(self, ticket_id: int, message_id: int) -> None:
        await self._execute(
            "UPDATE tickets SET supergroup_root_message_id = ? WHERE id = ?",
            (message_id, ticket_id),
        )

    async def set_ticket_intro(self, ticket_id: int, message_id: int) -> None:
        await self._execute(
            "UPDATE tickets SET user_intro_message_id = ? WHERE id = ?",
            (message_id, ticket_id),
        )

    async def append_user_update(self, ticket_id: int, addition: str) -> None:
        await self._execute(
            """
            UPDATE tickets
               SET user_updates = user_updates || ?,
                   last_activity_at = ?,
                   last_activity_role = 'user',
                   awaiting_admin_since = ?,
                   unanswered_updates = unanswered_updates + 1
             WHERE id = ?
            """,
            (addition, utc_now(), utc_now(), ticket_id),
        )

    async def append_system_update(self, ticket_id: int, addition: str) -> None:
        await self._execute(
            """
            UPDATE tickets
               SET user_updates = user_updates || ?,
                   last_activity_at = ?,
                   last_activity_role = 'system'
             WHERE id = ?
            """,
            (addition, utc_now(), ticket_id),
        )

    async def append_admin_update(self, ticket_id: int, addition: str) -> None:
        await self._execute(
            """
            UPDATE tickets
               SET user_updates = user_updates || ?,
                   last_activity_at = ?,
                   last_activity_role = 'admin'
             WHERE id = ?
            """,
            (addition, utc_now(), ticket_id),
        )

    async def set_notification_message(self, ticket_id: int, message_id: int | None) -> None:
        await self._execute(
            "UPDATE tickets SET notification_message_id = ? WHERE id = ?",
            (message_id, ticket_id),
        )

    async def clear_attention(self, ticket_id: int) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET awaiting_admin_since = NULL, unanswered_updates = 0,
                notification_message_id = NULL
            WHERE id = ?
            """,
            (ticket_id,),
        )

    async def delete_ticket(self, ticket_id: int) -> None:
        await self._execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

    async def clear_tickets(self) -> tuple[int, int]:
        """Delete every ticket and mapping, returning (tickets, messages).

        Used only by the explicit staging reset flag. Users and their
        verification state are kept; one-shot ``/new`` markers are cleared so
        the next message starts from a clean slate.
        """
        assert self.conn is not None
        async with self.conn.execute("SELECT COUNT(*) FROM tickets") as cursor:
            tickets = int((await cursor.fetchone())[0])
        async with self.conn.execute("SELECT COUNT(*) FROM ticket_messages") as cursor:
            messages = int((await cursor.fetchone())[0])
        await self.conn.execute("DELETE FROM ticket_messages")
        await self.conn.execute("DELETE FROM tickets")
        await self.conn.execute("UPDATE users SET force_new_until = NULL")
        await self.conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('tickets', 'ticket_messages')"
        )
        await self.conn.commit()
        logger.warning("cleared %s ticket(s) and %s ticket message(s)", tickets, messages)
        return tickets, messages

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        row = await self._fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return _from_row(Ticket, row) if row else None

    async def get_open_ticket_for_user(self, user_id: int) -> Ticket | None:
        row = await self._fetchone(
            "SELECT * FROM tickets WHERE user_id = ? AND status = 'open'", (user_id,)
        )
        return _from_row(Ticket, row) if row else None

    async def get_last_closed_ticket(self, user_id: int) -> Ticket | None:
        row = await self._fetchone(
            """
            SELECT * FROM tickets
            WHERE user_id = ? AND status = 'closed'
            ORDER BY closed_at DESC, id DESC LIMIT 1
            """,
            (user_id,),
        )
        return _from_row(Ticket, row) if row else None

    async def find_ticket_by_root(self, chat_id: int, message_id: int) -> Ticket | None:
        row = await self._fetchone(
            """
            SELECT * FROM tickets
            WHERE supergroup_chat_id = ? AND supergroup_root_message_id = ?
            """,
            (chat_id, message_id),
        )
        return _from_row(Ticket, row) if row else None

    async def find_mapping(self, chat_id: int, message_id: int) -> TicketMessage | None:
        row = await self._fetchone(
            "SELECT * FROM ticket_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        return _from_row(TicketMessage, row) if row else None

    async def find_ticket_by_message(self, chat_id: int, message_id: int) -> tuple[Ticket, TicketMessage | None] | None:
        ticket = await self.find_ticket_by_root(chat_id, message_id)
        if ticket is not None:
            return ticket, None
        mapping = await self.find_mapping(chat_id, message_id)
        if mapping is None:
            return None
        ticket = await self.get_ticket(mapping.ticket_id)
        if ticket is None:
            return None
        return ticket, mapping

    async def add_ticket_message(
        self,
        ticket_id: int,
        chat_id: int,
        message_id: int,
        direction: str,
        sender_user_id: int | None = None,
        parent_message_id: int | None = None,
        mirror_chat_id: int | None = None,
        mirror_message_id: int | None = None,
    ) -> int:
        mapping_id = await self._execute(
            """
            INSERT INTO ticket_messages (
                ticket_id, chat_id, message_id, direction, sender_user_id,
                parent_message_id, mirror_chat_id, mirror_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                chat_id,
                message_id,
                direction,
                sender_user_id,
                parent_message_id,
                mirror_chat_id,
                mirror_message_id,
                utc_now(),
            ),
        )
        return int(mapping_id)

    async def set_mirror(self, mapping_id: int, chat_id: int, message_id: int) -> None:
        await self._execute(
            "UPDATE ticket_messages SET mirror_chat_id = ?, mirror_message_id = ? WHERE id = ?",
            (chat_id, message_id, mapping_id),
        )

    async def set_mapping_edited(self, chat_id: int, message_id: int) -> None:
        await self._execute(
            "UPDATE ticket_messages SET edited_at = ? WHERE chat_id = ? AND message_id = ?",
            (utc_now(), chat_id, message_id),
        )

    async def touch_ticket(self, ticket_id: int, role: str) -> None:
        await self._execute(
            "UPDATE tickets SET last_activity_at = ?, last_activity_role = ? WHERE id = ?",
            (utc_now(), role, ticket_id),
        )

    async def close_ticket(
        self, ticket_id: int, by_user_id: int | None, reason: str | None = None
    ) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET status = 'closed', closed_at = ?, closed_by_user_id = ?,
                close_reason = ?, last_activity_at = ?, last_activity_role = 'system',
                awaiting_admin_since = NULL, unanswered_updates = 0,
                notification_message_id = NULL
            WHERE id = ? AND status = 'open'
            """,
            (utc_now(), by_user_id, reason, utc_now(), ticket_id),
        )

    async def reopen_ticket(self, ticket_id: int) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET status = 'open', closed_at = NULL, closed_by_user_id = NULL,
                close_reason = NULL, reopened_count = reopened_count + 1,
                last_activity_at = ?, last_activity_role = 'system',
                awaiting_admin_since = NULL, unanswered_updates = 0,
                notification_message_id = NULL
            WHERE id = ? AND status = 'closed'
            """,
            (utc_now(), ticket_id),
        )

    async def mark_unreachable(self, ticket_id: int, error: str) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET unreachable_at = COALESCE(unreachable_at, ?), last_delivery_error = ?
            WHERE id = ?
            """,
            (utc_now(), error[:500], ticket_id),
        )

    async def clear_unreachable(self, ticket_id: int) -> None:
        await self._execute(
            "UPDATE tickets SET unreachable_at = NULL, last_delivery_error = NULL WHERE id = ?",
            (ticket_id,),
        )

    async def list_open_tickets(self) -> list[TicketWithUser]:
        rows = await self._fetchall(
            """
            SELECT t.*, u.full_name AS _full_name, u.username AS _username
            FROM tickets t JOIN users u ON u.user_id = t.user_id
            WHERE t.status = 'open'
            ORDER BY t.last_activity_at ASC, t.id ASC
            """
        )
        return [
            TicketWithUser(
                # _from_row, not Ticket(**row): keeps the "0 = whole chat ->
                # None" normalization consistent with every other query.
                ticket=_from_row(Ticket, row),
                full_name=row["_full_name"],
                username=row["_username"],
            )
            for row in rows
        ]

    async def count_open_tickets(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS n FROM tickets WHERE status = 'open'")
        return int(row["n"]) if row else 0
