from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone

import coloredlogs


def install_logs(level: str = "INFO") -> None:
    coloredlogs.DEFAULT_FIELD_STYLES["levelname"]["color"] = "white"
    coloredlogs.install(
        level=getattr(logging, level.upper(), logging.INFO),
        fmt="%(asctime)s %(levelname)s %(module)s %(message)s",
    )


def utc_now() -> str:
    """Return a stable UTC timestamp used throughout the database."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def human_age(value: str | None) -> str:
    then = parse_timestamp(value)
    if then is None:
        return "unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def format_timestamp(value: str | None) -> str:
    then = parse_timestamp(value)
    if then is None:
        return "unknown"
    return then.strftime("%Y-%m-%d %H:%M UTC")


def truncate(value: str, limit: int = 120, *, preserve_newlines: bool = False) -> str:
    if preserve_newlines:
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        value = "\n".join(line.rstrip() for line in value.split("\n"))
        value = re.sub(r"\n{3,}", "\n\n", value)
    else:
        value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def normalize_secret(value: str) -> str:
    """Normalize a challenge answer so harmless formatting differences match."""
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"\s+", " ", value)
    return value.rstrip(".,!?;:'\"")
