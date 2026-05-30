from __future__ import annotations

from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient

from account_service.db import AccountRepository
from account_service.main import create_app


@pytest.fixture
def account_app(tmp_path):
    return create_app(repository=AccountRepository(tmp_path / "account.sqlite"))


@pytest.mark.asyncio
async def test_applies_transactions_and_computes_balance_out_of_order(
    account_app,
    event_payload,
    debit_payload,
):
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
    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        first = await client.post("/accounts/acct-123/transactions", json=event_payload)
        second = await client.post("/accounts/acct-123/transactions", json=event_payload)
        balance = await client.get("/accounts/acct-123/balance")

    assert first.status_code == 201
    assert second.status_code == 200
    assert balance.json()["balance"] == 150.0


@pytest.mark.asyncio
async def test_duplicate_event_id_with_different_payload_is_conflict(
    account_app,
    event_payload,
):
    conflicting_payload = deepcopy(event_payload)
    conflicting_payload["amount"] = 151.0

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        first = await client.post("/accounts/acct-123/transactions", json=event_payload)
        second = await client.post("/accounts/acct-123/transactions", json=conflicting_payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert "different transaction details" in second.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_second_currency_for_existing_account(account_app, event_payload):
    eur_payload = deepcopy(event_payload)
    eur_payload["eventId"] = "evt-eur"
    eur_payload["currency"] = "EUR"

    async with AsyncClient(
        transport=ASGITransport(app=account_app),
        base_url="http://account-service",
    ) as client:
        first = await client.post("/accounts/acct-123/transactions", json=event_payload)
        second = await client.post("/accounts/acct-123/transactions", json=eur_payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert "already uses currency USD" in second.json()["detail"]


@pytest.mark.asyncio
async def test_validation_rejects_bad_transaction(account_app, event_payload):
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
