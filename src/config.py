from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from src.utils import normalize_secret

APP_ENVS = {"production", "staging", "development"}


class ConfigError(ValueError):
    """Raised when the bot's environment is incomplete or contradictory."""


def _value(source: Mapping[str, str], name: str) -> str | None:
    raw = source.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _parse_int(source: Mapping[str, str], name: str, default: int | None = None) -> int:
    raw = _value(source, name)
    if raw is None:
        if default is None:
            raise ConfigError(f"{name} is required")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_ids(source: Mapping[str, str], name: str) -> frozenset[int]:
    raw = _value(source, name)
    if raw is None:
        return frozenset()
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-integer id: {item!r}") from exc
    return frozenset(ids)


def _parse_list(source: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = _value(source, name)
    if raw is None:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    app_env: str
    bot_token: str
    support_chat_id: int
    tickets_topic_id: int  # 0 means the whole group (no forum topic)
    admin_ids: frozenset[int]
    secret_question: str
    secrets: tuple[str, ...]
    db_path: Path
    log_level: str = "INFO"
    timezone: str = "UTC"
    verify_max_attempts: int = 3
    verify_cooldown_min: int = 10
    reopen_window_days: int = 7
    auto_close_after_days: int = 14
    retention_days: int = 365
    max_ticket_file_mb: int = 50

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def uses_topic(self) -> bool:
        return self.tickets_topic_id > 0

    @property
    def tickets_url_base(self) -> str:
        """Base URL for private-supergroup message links (t.me/c/...)."""
        internal_id = str(self.support_chat_id)
        if internal_id.startswith("-100"):
            internal_id = internal_id[4:]
        return f"https://t.me/c/{internal_id}"

    def message_url(self, message_id: int, topic_id: int | None = None) -> str:
        """Link to a message; the topic defaults to the configured one."""
        thread = topic_id if topic_id is not None else self.tickets_topic_id
        if thread:
            return f"{self.tickets_url_base}/{thread}/{message_id}"
        return f"{self.tickets_url_base}/{message_id}"

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Config":
        if environ is None:
            path = Path(env_file or ".env")
            if env_file is not None and not path.exists():
                raise ConfigError(f"Environment file not found: {path}")
            load_dotenv(dotenv_path=path, override=False)
            source: Mapping[str, str] = os.environ
        else:
            source = environ

        app_env = (_value(source, "APP_ENV") or "production").lower()
        if app_env not in APP_ENVS:
            raise ConfigError(f"APP_ENV must be one of {sorted(APP_ENVS)}, got {app_env!r}")

        token = _value(source, "BOT_TOKEN")
        if token is None:
            raise ConfigError("BOT_TOKEN is required")

        support_raw = _value(source, "SUPPORT_CHAT_ID")
        legacy_raw = _value(source, "SUPERGROUP_ID")
        if support_raw is None and legacy_raw is None:
            raise ConfigError("SUPPORT_CHAT_ID is required (legacy SUPERGROUP_ID is accepted)")
        if support_raw is not None:
            try:
                support_chat_id = int(support_raw)
            except ValueError as exc:
                raise ConfigError("SUPPORT_CHAT_ID must be an integer") from exc
            if legacy_raw is not None:
                try:
                    legacy_id = int(legacy_raw)
                except ValueError as exc:
                    raise ConfigError("SUPERGROUP_ID must be an integer") from exc
                if legacy_id != support_chat_id:
                    raise ConfigError(
                        "SUPPORT_CHAT_ID and legacy SUPERGROUP_ID disagree; remove one"
                    )
        else:
            try:
                support_chat_id = int(legacy_raw)  # type: ignore[arg-type]
            except ValueError as exc:
                raise ConfigError("SUPERGROUP_ID must be an integer") from exc

        if support_chat_id >= 0:
            raise ConfigError("SUPPORT_CHAT_ID must be a negative supergroup id")

        # 0 (or unset) means the support group itself is the inbox: no forum
        # topic is used. At startup this is checked against the real chat type.
        topic_id = _parse_int(source, "TICKETS_TOPIC_ID", 0)
        if topic_id < 0:
            raise ConfigError("TICKETS_TOPIC_ID must be a non-negative integer")

        question = _value(source, "SECRET_QUESTION")
        if question is None:
            raise ConfigError("SECRET_QUESTION is required")

        secrets = tuple(
            normalized
            for normalized in (normalize_secret(item) for item in _parse_list(source, "SECRETS"))
            if normalized
        )
        if not secrets:
            raise ConfigError("SECRETS must contain at least one accepted answer")

        db_path = Path(_value(source, "DB_PATH") or "data/beaver.db").expanduser()
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path

        return cls(
            app_env=app_env,
            bot_token=token,
            support_chat_id=support_chat_id,
            tickets_topic_id=topic_id,
            admin_ids=_parse_ids(source, "ADMIN_IDS"),
            secret_question=question,
            secrets=secrets,
            db_path=db_path,
            log_level=(_value(source, "LOG_LEVEL") or "INFO").upper(),
            timezone=_value(source, "TIMEZONE") or "UTC",
            verify_max_attempts=_parse_int(source, "VERIFY_MAX_ATTEMPTS", 3),
            verify_cooldown_min=_parse_int(source, "VERIFY_COOLDOWN_MIN", 10),
            reopen_window_days=_parse_int(source, "REOPEN_WINDOW_DAYS", 7),
            auto_close_after_days=_parse_int(source, "AUTO_CLOSE_AFTER_DAYS", 14),
            retention_days=_parse_int(source, "RETENTION_DAYS", 365),
            max_ticket_file_mb=_parse_int(source, "MAX_TICKET_FILE_MB", 50),
        )
