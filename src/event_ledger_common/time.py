"""UTC timestamp normalization helpers.

Engineering view: every timestamp conversion flows through this module so the
services do not develop competing time formats.
Business view: correct ordering of financial events depends on comparable,
timezone-aware event times.
"""

from __future__ import annotations

from datetime import UTC, datetime


def require_aware_utc(value: datetime) -> datetime:
    """Return a UTC datetime, rejecting timezone-naive input.

    Architecture view: the ledger treats timestamp ambiguity as invalid input
    because out-of-order tolerance relies on deterministic event ordering.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return the current UTC timestamp.

    Engineering view: audit timestamps are generated consistently across both
    services.
    """

    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Serialize a datetime as an ISO 8601 UTC string with Z suffix.

    Business view: API responses and database values use a single timestamp
    representation, which makes audit trails easier to read.
    """

    normalized = require_aware_utc(value)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp and normalize it to UTC.

    Engineering view: repository mappers call this when reconstructing typed
    response models from SQLite text columns.
    """

    return require_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
