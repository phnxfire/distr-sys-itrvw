from __future__ import annotations

from datetime import UTC, datetime


def require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    normalized = require_aware_utc(value)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return require_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
