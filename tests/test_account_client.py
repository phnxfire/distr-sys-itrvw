from __future__ import annotations

import httpx
import pytest

from event_ledger_common.contracts import EventPayload
from gateway_service.account_client import AccountServiceUnavailableError, HttpAccountClient


@pytest.mark.asyncio
async def test_account_client_retries_server_errors_and_propagates_trace_id(event_payload):
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={"detail": "temporary outage"})
        return httpx.Response(201, json={"ok": True})

    client = HttpAccountClient(
        base_url="http://account-service",
        max_attempts=3,
        backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    result = await client.apply_transaction(
        EventPayload.model_validate(event_payload),
        trace_id="trace-retry-123",
    )

    assert result == {"ok": True}
    assert len(calls) == 3
    assert {request.headers["X-Trace-Id"] for request in calls} == {"trace-retry-123"}


@pytest.mark.asyncio
async def test_account_client_returns_unavailable_after_bounded_retries(event_payload):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "still down"})

    client = HttpAccountClient(
        base_url="http://account-service",
        max_attempts=2,
        backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AccountServiceUnavailableError):
        await client.apply_transaction(
            EventPayload.model_validate(event_payload),
            trace_id="trace-failure-123",
        )

    assert calls == 2
