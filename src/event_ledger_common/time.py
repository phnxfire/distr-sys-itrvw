"""UTC timestamp normalization helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def require_aware_utc(value: datetime) -> datetime:
    """Return a UTC datetime, rejecting timezone-naive input."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Serialize a datetime as an ISO 8601 UTC string with Z suffix."""

    normalized = require_aware_utc(value)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp and normalize it to UTC."""

    return require_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
