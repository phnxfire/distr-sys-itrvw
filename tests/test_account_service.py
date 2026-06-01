"""Account Service tests for the internal account-state boundary.

Business view: these tests prove account balances are correct, duplicates do
not change money twice, and conflicting financial facts are rejected.
Architecture view: the tests exercise Account Service directly because it owns
transactions and balances independent of Gateway.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient

from account_service.db import AccountRepository
from account_service.main import create_app


@pytest.fixture
def account_app(tmp_path):
    """Create an Account Service app backed by an isolated SQLite file.

    Engineering view: each test gets fresh embedded persistence so account
    state never leaks between scenarios.
    """

    return create_app(repository=AccountRepository(tmp_path / "account.sqlite"))


@pytest.mark.asyncio
async def test_applies_transactions_and_computes_balance_out_of_order(
    account_app,
    event_payload,
    debit_payload,
):
    """Verify balance and history are correct when events arrive out of order.

    Business view: the account result must depend on transaction facts, not
    upstream delivery order.
    """

    older_debit = deepcopy(debit_payload)
    older_debit["eventTimestamp"] = "2026-05-15T13:02:11Z"

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        debit_response = await client.post(
            f"/accounts/{older_debit['accountId']}/transactions",
            json=older_debit,
        )
        credit_response = await client.post(
            f"/accounts/{event_payload['accountId']}/transactions",
            json=event_payload,
        )
        balance_response = await client.get("/accounts/acct-123/balance")
        account_response = await client.get("/accounts/acct-123")

    assert debit_response.status_code == 201
    assert credit_response.status_code == 201
    assert balance_response.status_code == 200
    assert balance_response.json()["balance"] == 124.5

    transactions = account_response.json()["recentTransactions"]
    assert [event["eventId"] for event in transactions] == ["evt-002", "evt-001"]


@pytest.mark.asyncio
async def test_duplicate_transaction_does_not_change_balance(account_app, event_payload):
    """Verify duplicate transaction replays do not change account balance.

    Business view: duplicate delivery must not double-credit or double-debit a
    customer account.
    """

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        first = await client.post("/accounts/acct-123/transactions", json=event_payload)
        second = await client.post("/accounts/acct-123/transactions", json=event_payload)
        balance = await client.get("/accounts/acct-123/balance")
        metrics = await client.get("/metrics")

    assert first.status_code == 201
    assert second.status_code == 200
    assert balance.json()["balance"] == 150.0
    assert metrics.json()["domainEvents"]["account.transactions.applied"] == 1
    assert metrics.json()["domainEvents"]["account.transactions.duplicate_replay"] == 1


@pytest.mark.asyncio
async def test_duplicate_event_id_with_different_payload_is_conflict(
    account_app,
    event_payload,
):
    """Verify conflicting transaction payloads for one eventId are rejected.

    Business view: a reused eventId with different amount or metadata is an
    idempotency conflict, not a valid update.
    """

    conflicting_payload = deepcopy(event_payload)
    conflicting_payload["amount"] = 151.0

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        first = await client.post("/accounts/acct-123/transactions", json=event_payload)
        second = await client.post("/accounts/acct-123/transactions", json=conflicting_payload)
        metrics = await client.get("/metrics")

    assert first.status_code == 201
    assert second.status_code == 409
    assert "different transaction details" in second.json()["detail"]
    assert metrics.json()["domainEvents"]["account.transactions.idempotency_conflict"] == 1


@pytest.mark.asyncio
async def test_rejects_second_currency_for_existing_account(account_app, event_payload):
    """Verify an account cannot mix currencies in this ledger model.

    Business view: the simplified balance model cannot safely sum different
    currencies into one number.
    """

    eur_payload = deepcopy(event_payload)
    eur_payload["eventId"] = "evt-eur"
    eur_payload["currency"] = "EUR"

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        first = await client.post("/accounts/acct-123/transactions", json=event_payload)
        second = await client.post("/accounts/acct-123/transactions", json=eur_payload)
        metrics = await client.get("/metrics")

    assert first.status_code == 201
    assert second.status_code == 409
    assert "already uses currency USD" in second.json()["detail"]
    assert metrics.json()["domainEvents"]["account.transactions.currency_conflict"] == 1


@pytest.mark.asyncio
async def test_validation_rejects_bad_transaction(account_app, event_payload):
    """Verify invalid transaction amounts fail validation.

    Engineering view: invalid financial input is rejected by the shared contract
    before repository code can persist it.
    """

    bad_payload = deepcopy(event_payload)
    bad_payload["amount"] = 0

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        response = await client.post("/accounts/acct-123/transactions", json=bad_payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_health_and_metrics(account_app, event_payload):
    """Verify Account Service health and metrics endpoints return diagnostics.

    Operations view: health and metrics expose service readiness and business
    outcomes without external tooling.
    """

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        health = await client.get("/health")
        await client.post("/accounts/acct-123/transactions", json=event_payload)
        metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert "POST /accounts/{account_id}/transactions" in metrics.json()["requests"]
    assert metrics.json()["domainEvents"]["account.transactions.applied"] == 1


@pytest.mark.asyncio
async def test_traceparent_is_accepted_and_echoed(account_app):
    """Verify Account Service accepts W3C traceparent and echoes trace context.

    Architecture view: the internal service participates in distributed trace
    propagation instead of becoming an observability dead end.
    """

    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    traceparent = f"00-{trace_id}-00f067aa0ba902b7-01"

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        response = await client.get("/health", headers={"traceparent": traceparent})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == trace_id
    assert response.headers["traceparent"].startswith(f"00-{trace_id}-")
