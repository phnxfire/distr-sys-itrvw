from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from event_ledger_common.contracts import AccountDetailsResponse, BalanceResponse
from gateway_service.account_client import AccountServiceUnavailableError
from gateway_service.db import GatewayRepository
from gateway_service.main import create_app


class FakeAccountClient:
    def __init__(self) -> None:
        """Initialize a controllable Account Service test double."""

        self.apply_calls: list[tuple[str, str]] = []
        self.balance_calls: list[tuple[str, str]] = []
        self.fail_apply = False
        self.fail_reads = False

    async def apply_transaction(self, event, trace_id: str):
        """Record or reject a Gateway transaction apply call."""

        if self.fail_apply:
            raise AccountServiceUnavailableError("down")
        self.apply_calls.append((event.event_id, trace_id))
        return {"ok": True}

    async def get_balance(self, account_id: str, trace_id: str) -> BalanceResponse:
        """Return a fixed balance or simulate a read outage."""

        if self.fail_reads:
            raise AccountServiceUnavailableError("down")
        self.balance_calls.append((account_id, trace_id))
        return BalanceResponse(accountId=account_id, balance=Decimal("150.0"), currency="USD")

    async def get_account(self, account_id: str, trace_id: str) -> AccountDetailsResponse:
        """Return fixed account details or simulate a read outage."""

        if self.fail_reads:
            raise AccountServiceUnavailableError("down")
        return AccountDetailsResponse(
            accountId=account_id,
            balance=Decimal("150.0"),
            currency="USD",
            recentTransactions=[],
        )


@pytest.fixture
def fake_account_client() -> FakeAccountClient:
    """Provide a fresh Account Service test double."""

    return FakeAccountClient()


@pytest.fixture
def gateway_app(tmp_path, fake_account_client):
    """Create a Gateway app backed by an isolated SQLite file."""

    return create_app(
        repository=GatewayRepository(tmp_path / "gateway.sqlite"),
        account_client=fake_account_client,
    )


@pytest.mark.asyncio
async def test_submit_event_and_duplicate_replay_do_not_reapply(
    gateway_app,
    fake_account_client,
    event_payload,
):
    """Verify duplicate submissions return the original event without reapplying."""

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        first = await client.post("/events", json=event_payload)
        second = await client.post("/events", json=event_payload)
        listed = await client.get("/events", params={"account": "acct-123"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert [call[0] for call in fake_account_client.apply_calls] == ["evt-001"]


@pytest.mark.asyncio
async def test_duplicate_event_id_with_different_payload_is_conflict(
    gateway_app,
    event_payload,
):
    """Verify eventId reuse with different details is rejected."""

    conflicting_payload = deepcopy(event_payload)
    conflicting_payload["amount"] = 999.0

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        first = await client.post("/events", json=event_payload)
        second = await client.post("/events", json=conflicting_payload)

    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_event_listing_is_chronological_not_arrival_order(
    gateway_app,
    event_payload,
    debit_payload,
):
    """Verify Gateway event listing is ordered by eventTimestamp."""

    earlier_payload = deepcopy(debit_payload)
    earlier_payload["eventTimestamp"] = "2026-05-15T13:02:11Z"

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        later = await client.post("/events", json=event_payload)
        earlier = await client.post("/events", json=earlier_payload)
        listed = await client.get("/events", params={"account": "acct-123"})

    assert later.status_code == 201
    assert earlier.status_code == 201
    assert [item["eventId"] for item in listed.json()] == ["evt-002", "evt-001"]


@pytest.mark.asyncio
async def test_validation_rejects_bad_event(gateway_app, event_payload):
    """Verify invalid event types are rejected by request validation."""

    bad_payload = deepcopy(event_payload)
    bad_payload["type"] = "TRANSFER"

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        response = await client.post("/events", json=bad_payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_account_service_failure_returns_503_and_event_reads_still_work(
    gateway_app,
    fake_account_client,
    event_payload,
    debit_payload,
):
    """Verify write degradation while Gateway-owned reads remain available."""

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        accepted = await client.post("/events", json=event_payload)
        fake_account_client.fail_apply = True
        rejected = await client.post("/events", json=debit_payload)
        existing_event = await client.get("/events/evt-001")
        listed = await client.get("/events", params={"account": "acct-123"})

    assert accepted.status_code == 201
    assert rejected.status_code == 503
    assert "not accepted" in rejected.json()["detail"]
    assert existing_event.status_code == 200
    assert len(listed.json()) == 1


@pytest.mark.asyncio
async def test_balance_proxy_returns_503_when_account_service_is_down(
    gateway_app,
    fake_account_client,
):
    """Verify Account Service read outages become clear Gateway 503 responses."""

    fake_account_client.fail_reads = True

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        response = await client.get("/accounts/acct-123/balance")

    assert response.status_code == 503
    assert "balance cannot be retrieved" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trace_id_is_propagated_to_account_service(
    gateway_app,
    fake_account_client,
    event_payload,
):
    """Verify Gateway propagates caller trace IDs to Account Service calls."""

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        response = await client.post(
            "/events",
            json=event_payload,
            headers={"X-Trace-Id": "trace-test-123"},
        )

    assert response.status_code == 201
    assert response.headers["X-Trace-Id"] == "trace-test-123"
    assert fake_account_client.apply_calls == [("evt-001", "trace-test-123")]


@pytest.mark.asyncio
async def test_traceparent_is_accepted_and_echoed(
    gateway_app,
    fake_account_client,
    event_payload,
):
    """Verify Gateway accepts W3C traceparent and uses its trace ID."""

    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    traceparent = f"00-{trace_id}-00f067aa0ba902b7-01"

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        response = await client.post(
            "/events",
            json=event_payload,
            headers={"traceparent": traceparent},
        )

    assert response.status_code == 201
    assert response.headers["X-Trace-Id"] == trace_id
    assert response.headers["traceparent"].startswith(f"00-{trace_id}-")
    assert fake_account_client.apply_calls == [("evt-001", trace_id)]


@pytest.mark.asyncio
async def test_health_and_metrics(gateway_app, event_payload):
    """Verify Gateway health and metrics endpoints report usable diagnostics."""

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        health = await client.get("/health")
        await client.post("/events", json=event_payload)
        metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert "POST /events" in metrics.json()["requests"]
