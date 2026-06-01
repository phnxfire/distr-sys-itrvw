"""Gateway service tests for public API behavior and failure handling.

Architecture view: these tests verify that Gateway owns event records while
delegating account state to a replaceable Account Service boundary.
Business view: the scenarios cover duplicate delivery, out-of-order events,
downstream outages, and traceability for submitted financial events.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from event_ledger_common.contracts import AccountDetailsResponse, BalanceResponse
from gateway_service.account_client import (
    AccountServiceCircuitOpenError,
    AccountServiceUnavailableError,
)
from gateway_service.db import GatewayRepository
from gateway_service.main import create_app


class FakeAccountClient:
    """Controllable Account Service adapter used to isolate Gateway behavior.

    Engineering view: this fake lets Gateway tests simulate success, outage, and
    circuit-open behavior without starting a second ASGI application.
    """

    def __init__(self) -> None:
        """Initialize a clean test double with no recorded downstream calls."""

        self.apply_calls: list[tuple[str, str]] = []
        self.balance_calls: list[tuple[str, str]] = []
        self.fail_apply = False
        self.apply_circuit_open = False
        self.fail_reads = False

    async def apply_transaction(self, event, trace_id: str):
        """Record or reject a Gateway transaction apply call.

        Business view: tests use this to prove duplicate Gateway submissions do
        not reapply money through Account Service.
        """

        if self.apply_circuit_open:
            raise AccountServiceCircuitOpenError("circuit open")
        if self.fail_apply:
            raise AccountServiceUnavailableError("down")
        self.apply_calls.append((event.event_id, trace_id))
        return {"ok": True}

    async def get_balance(self, account_id: str, trace_id: str) -> BalanceResponse:
        """Return a fixed balance or simulate a read outage.

        Architecture view: Gateway balance endpoints remain proxies, not local
        balance calculators.
        """

        if self.fail_reads:
            raise AccountServiceUnavailableError("down")
        self.balance_calls.append((account_id, trace_id))
        return BalanceResponse(accountId=account_id, balance=Decimal("150.0"), currency="USD")

    async def get_account(self, account_id: str, trace_id: str) -> AccountDetailsResponse:
        """Return fixed account details or simulate a read outage.

        Engineering view: the fake keeps account-detail tests focused on
        Gateway error translation and trace propagation.
        """

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
    """Provide a fresh Account Service test double.

    Engineering view: a new fake per test prevents cross-test call history from
    hiding idempotency bugs.
    """

    return FakeAccountClient()


@pytest.fixture
def gateway_app(tmp_path, fake_account_client):
    """Create a Gateway app backed by an isolated SQLite file.

    Architecture view: each test uses real Gateway persistence while replacing
    only the Account Service boundary.
    """

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
    """Verify duplicate submissions return the original event without reapplying.

    Business view: repeated upstream delivery must not call Account Service a
    second time or alter customer balances.
    """

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        first = await client.post("/events", json=event_payload)
        second = await client.post("/events", json=event_payload)
        listed = await client.get("/events", params={"account": "acct-123"})
        metrics = await client.get("/metrics")

    assert first.status_code == 201
    assert second.status_code == 200
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert [call[0] for call in fake_account_client.apply_calls] == ["evt-001"]
    assert metrics.json()["domainEvents"]["gateway.events.accepted"] == 1
    assert metrics.json()["domainEvents"]["gateway.events.duplicate_replay"] == 1


@pytest.mark.asyncio
async def test_duplicate_event_id_with_different_payload_is_conflict(
    gateway_app,
    event_payload,
):
    """Verify eventId reuse with different details is rejected.

    Business view: conflicting use of the same eventId indicates a producer or
    replay problem and must not be accepted silently.
    """

    conflicting_payload = deepcopy(event_payload)
    conflicting_payload["amount"] = 999.0

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        first = await client.post("/events", json=event_payload)
        second = await client.post("/events", json=conflicting_payload)
        metrics = await client.get("/metrics")

    assert first.status_code == 201
    assert second.status_code == 409
    assert metrics.json()["domainEvents"]["gateway.events.idempotency_conflict"] == 1


@pytest.mark.asyncio
async def test_event_listing_is_chronological_not_arrival_order(
    gateway_app,
    event_payload,
    debit_payload,
):
    """Verify Gateway event listing is ordered by eventTimestamp.

    Architecture view: Gateway stores event time separately from arrival time so
    upstream synchronization issues do not leak into client history.
    """

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
    """Verify invalid event types are rejected by request validation.

    Engineering view: FastAPI/Pydantic contract validation protects downstream
    services from unsupported event types.
    """

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
    """Verify write degradation while Gateway-owned reads remain available.

    Graceful degradation view: when Account Service is unavailable, Gateway
    rejects new writes but still serves its own event history.
    """

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        accepted = await client.post("/events", json=event_payload)
        fake_account_client.fail_apply = True
        rejected = await client.post("/events", json=debit_payload)
        existing_event = await client.get("/events/evt-001")
        listed = await client.get("/events", params={"account": "acct-123"})
        metrics = await client.get("/metrics")

    assert accepted.status_code == 201
    assert rejected.status_code == 503
    assert "not accepted" in rejected.json()["detail"]
    assert existing_event.status_code == 200
    assert len(listed.json()) == 1
    assert metrics.json()["domainEvents"]["gateway.account_service.unavailable"] == 1


@pytest.mark.asyncio
async def test_account_service_circuit_open_is_observable(
    gateway_app,
    fake_account_client,
    event_payload,
):
    """Verify Gateway exposes circuit-open downstream protection as a domain metric.

    Operations view: a circuit-open signal is different from a raw failed call
    and should be visible to reviewers and alerting systems.
    """

    fake_account_client.apply_circuit_open = True

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        response = await client.post("/events", json=event_payload)
        metrics = await client.get("/metrics")

    assert response.status_code == 503
    assert "circuit breaker is open" in response.json()["detail"]
    assert metrics.json()["domainEvents"]["gateway.account_service.circuit_open"] == 1


@pytest.mark.asyncio
async def test_balance_proxy_returns_503_when_account_service_is_down(
    gateway_app,
    fake_account_client,
):
    """Verify Account Service read outages become clear Gateway 503 responses.

    Business view: clients receive a clear unavailable account-state response
    instead of stale or locally guessed balances.
    """

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
    """Verify Gateway propagates caller trace IDs to Account Service calls.

    Operations view: one client request can be followed across the public and
    internal service boundary.
    """

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
    """Verify Gateway accepts W3C traceparent and uses its trace ID.

    Architecture view: supporting traceparent keeps the design aligned with
    OpenTelemetry-style distributed tracing.
    """

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
    """Verify Gateway health and metrics endpoints report usable diagnostics.

    Operations view: the public service exposes readiness and business outcome
    counters without needing external infrastructure.
    """

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
    assert metrics.json()["domainEvents"]["gateway.events.accepted"] == 1
