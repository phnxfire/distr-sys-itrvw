"""Persistence boundary for Gateway-owned event records."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from event_ledger_common.contracts import EventPayload, EventRecord, EventStatus
from event_ledger_common.time import format_timestamp, parse_timestamp, utc_now


class DuplicateEventConflictError(Exception):
    """Raised when an eventId is reused with different event details."""

    pass


class GatewayRepository:
    """SQLite-backed repository owned exclusively by the Event Gateway."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """Open the SQLite database and initialize the event schema."""

        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # The repository serializes access to one SQLite connection per app instance.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        """Create the service-owned event schema and account listing index."""

        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    event_timestamp TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_account_timestamp
                ON events(account_id, event_timestamp, event_id)
                """
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        self._conn.close()

    def health_check(self) -> bool:
        """Verify that the repository can execute a simple database query."""

        with self._lock:
            self._conn.execute("SELECT 1").fetchone()
        return True

    def insert_applied_event(self, event: EventPayload) -> tuple[EventRecord, bool]:
        """Persist an event after Account Service has applied the transaction.

        Returns the stored event and a boolean indicating whether this call
        inserted a new row. Replays with identical payloads are returned without
        writing another event.
        """

        with self._lock:
            existing = self.get_event(event.event_id)
            if existing:
                if not _same_event(existing, event):
                    raise DuplicateEventConflictError(event.event_id)
                return existing, False

            now = format_timestamp(utc_now())
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO events (
                        event_id,
                        account_id,
                        event_type,
                        amount,
                        currency,
                        event_timestamp,
                        metadata_json,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.account_id,
                        event.type,
                        # Store money as text to avoid SQLite numeric coercion.
                        str(event.amount),
                        event.currency,
                        format_timestamp(event.event_timestamp),
                        _metadata_json(event.metadata),
                        EventStatus.APPLIED.value,
                        now,
                        now,
                    ),
                )

            inserted = self.get_event(event.event_id)
            if inserted is None:
                raise RuntimeError("event insert was not readable")
            return inserted, True

    def get_event(self, event_id: str) -> EventRecord | None:
        """Fetch one Gateway event by idempotency key."""

        row = self._conn.execute(
            """
            SELECT event_id, account_id, event_type, amount, currency, event_timestamp,
                   metadata_json, status, created_at, updated_at
            FROM events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        return _event_from_row(row) if row else None

    def list_events_for_account(self, account_id: str) -> list[EventRecord]:
        """List account events ordered by original event time, not arrival time."""

        rows = self._conn.execute(
            """
            SELECT event_id, account_id, event_type, amount, currency, event_timestamp,
                   metadata_json, status, created_at, updated_at
            FROM events
            WHERE account_id = ?
            ORDER BY event_timestamp ASC, event_id ASC
            """,
            (account_id,),
        ).fetchall()
        return [_event_from_row(row) for row in rows]


def _metadata_json(value: dict[str, Any]) -> str:
    """Serialize metadata deterministically for storage and comparison."""

    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    """Map a SQLite event row into the API response contract."""

    return EventRecord(
        eventId=row["event_id"],
        accountId=row["account_id"],
        type=row["event_type"],
        amount=Decimal(row["amount"]),
        currency=row["currency"],
        eventTimestamp=parse_timestamp(row["event_timestamp"]),
        metadata=json.loads(row["metadata_json"]),
        status=row["status"],
        createdAt=parse_timestamp(row["created_at"]),
        updatedAt=parse_timestamp(row["updated_at"]),
    )


def _same_event(existing: EventPayload, incoming: EventPayload) -> bool:
    """Return whether two event payloads represent the same business event."""

    return (
        existing.event_id == incoming.event_id
        and existing.account_id == incoming.account_id
        and existing.type == incoming.type
        and existing.amount == incoming.amount
        and existing.currency == incoming.currency
        and format_timestamp(existing.event_timestamp) == format_timestamp(incoming.event_timestamp)
        and existing.metadata == incoming.metadata
    )
