"""Shared pytest fixtures for Event Ledger behavior tests.

Engineering view: central fixtures keep event payloads consistent across
Gateway, Account Service, client, and integration tests.
Business view: the default payloads model realistic credit and debit events for
the same account, making idempotency and balance assertions easy to understand.
"""

from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.fixture
def event_payload() -> dict:
    """Return a valid CREDIT event payload used by Gateway and Account tests.

    Business view: this is the canonical successful money-in event used to prove
    the happy path across the system.
    """

    return {
        "eventId": "evt-001",
        "accountId": "acct-123",
        "type": "CREDIT",
        "amount": 150.0,
        "currency": "USD",
        "eventTimestamp": "2026-05-15T14:02:11Z",
        "metadata": {"source": "mainframe-batch", "batchId": "B-9042"},
    }


@pytest.fixture
def debit_payload(event_payload: dict) -> dict:
    """Return a valid DEBIT event payload for the same account.

    Business view: pairing a debit with the default credit verifies net balance
    behavior and out-of-order event handling.
    """

    payload = deepcopy(event_payload)
    payload.update(
        {
            "eventId": "evt-002",
            "type": "DEBIT",
            "amount": 25.5,
            "eventTimestamp": "2026-05-15T15:02:11Z",
        }
    )
    return payload
