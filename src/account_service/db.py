"""Persistence boundary for Account Service transactions and balances."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from event_ledger_common.contracts import (
    AccountDetailsResponse,
    BalanceResponse,
    EventPayload,
    TransactionRecord,
)
from event_ledger_common.time import format_timestamp, parse_timestamp, utc_now


class DuplicateEventConflictError(Exception):
    """Raised when an eventId is reused with different transaction details."""

    pass


class AccountCurrencyMismatchError(Exception):
    """Raised when an account receives transactions in multiple currencies."""

    pass


class AccountRepository:
    """SQLite-backed repository owned exclusively by the Account Service."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """Open the SQLite database and initialize the transaction schema."""

        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # FastAPI may use multiple worker threads in-process; repository methods
        # serialize access around this shared SQLite connection.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        """Create the service-owned schema and query indexes."""

        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    event_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    event_timestamp TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_transactions_account_timestamp
                ON transactions(account_id, event_timestamp, event_id)
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

    def apply_transaction(self, event: EventPayload) -> tuple[TransactionRecord, bool]:
        """Apply a transaction exactly once by eventId.

        Returns the stored transaction and a boolean indicating whether this call
        inserted a new row. A replay with identical details is considered
        successful and returns ``created=False``.
        """

        with self._lock:
            existing = self.get_transaction(event.event_id)
            if existing:
                if not _same_event(existing, event):
                    raise DuplicateEventConflictError(event.event_id)
                return existing, False

            account_currency = self._account_currency(event.account_id)
            if account_currency is not None and account_currency != event.currency:
                raise AccountCurrencyMismatchError(
                    f"account {event.account_id} already uses currency {account_currency}"
                )

            now = format_timestamp(utc_now())
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO transactions (
                        event_id,
                        account_id,
                        event_type,
                        amount,
                        currency,
                        event_timestamp,
                        metadata_json,
                        applied_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                        now,
                    ),
                )

            inserted = self.get_transaction(event.event_id)
            if inserted is None:
                raise RuntimeError("transaction insert was not readable")
            return inserted, True

    def get_transaction(self, event_id: str) -> TransactionRecord | None:
        """Fetch one transaction by idempotency key."""

        row = self._conn.execute(
            """
            SELECT event_id, account_id, event_type, amount, currency, event_timestamp,
                   metadata_json, applied_at
            FROM transactions
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        return _transaction_from_row(row) if row else None

    def get_balance(self, account_id: str) -> BalanceResponse:
        """Calculate current balance from all applied account transactions."""

        rows = self._conn.execute(
            """
            SELECT event_type, amount, currency
            FROM transactions
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchall()
        balance = Decimal("0")
        currency: str | None = None
        for row in rows:
            amount = Decimal(row["amount"])
            balance += amount if row["event_type"] == "CREDIT" else -amount
            currency = currency or row["currency"]
        return BalanceResponse(accountId=account_id, balance=balance, currency=currency)

    def get_account_details(self, account_id: str, limit: int = 25) -> AccountDetailsResponse:
        """Return account balance with a bounded chronological transaction history."""

        balance = self.get_balance(account_id)
        rows = self._conn.execute(
            """
            SELECT event_id, account_id, event_type, amount, currency, event_timestamp,
                   metadata_json, applied_at
            FROM transactions
            WHERE account_id = ?
            ORDER BY event_timestamp ASC, event_id ASC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
        return AccountDetailsResponse(
            accountId=account_id,
            balance=balance.balance,
            currency=balance.currency,
            recentTransactions=[_transaction_from_row(row) for row in rows],
        )

    def _account_currency(self, account_id: str) -> str | None:
        """Return the existing account currency, if the account has transactions."""

        row = self._conn.execute(
            """
            SELECT currency
            FROM transactions
            WHERE account_id = ?
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        return row["currency"] if row else None


def _metadata_json(value: dict[str, Any]) -> str:
    """Serialize metadata deterministically for storage and comparison."""

    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _transaction_from_row(row: sqlite3.Row) -> TransactionRecord:
    """Map a SQLite transaction row into the API response contract."""

    return TransactionRecord(
        eventId=row["event_id"],
        accountId=row["account_id"],
        type=row["event_type"],
        amount=Decimal(row["amount"]),
        currency=row["currency"],
        eventTimestamp=parse_timestamp(row["event_timestamp"]),
        metadata=json.loads(row["metadata_json"]),
        appliedAt=parse_timestamp(row["applied_at"]),
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
