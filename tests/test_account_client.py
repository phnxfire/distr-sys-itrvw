"""Account Service client resiliency and trace propagation tests.

Engineering view: HTTPX mock transports make retry, timeout-adjacent behavior,
headers, and circuit breaker state deterministic without real network calls.
Architecture view: these tests prove the Gateway's downstream adapter carries
the resiliency contract for every Account Service operation.
"""

from __future__ import annotations

import httpx
import pytest

from event_ledger_common.contracts import EventPayload
from gateway_service.account_client import (
    AccountServiceCircuitOpenError,
    AccountServiceUnavailableError,
    HttpAccountClient,
)


@pytest.mark.asyncio
async def test_account_client_retries_server_errors_and_propagates_trace_id(event_payload):
    """Verify retriable 5xx responses preserve trace IDs across attempts.

    Operations view: retry attempts must stay correlated to the original client
    request so failures remain traceable.
    """

    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return two temporary failures followed by success.

        Engineering view: the sequence proves bounded retry behavior without
        sleeping or depending on external services.
        """

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
async def test_account_client_propagates_traceparent_for_w3c_trace_id(event_payload):
    """Verify W3C-compatible trace IDs are propagated as traceparent headers.

    Architecture view: W3C propagation keeps the design compatible with a later
    OpenTelemetry collector.
    """

    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    captured_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        """Capture a successful outbound Account Service request.

        Engineering view: capturing the request directly verifies headers at the
        client boundary.
        """

        nonlocal captured_request
        captured_request = request
        return httpx.Response(201, json={"ok": True})

    client = HttpAccountClient(
        base_url="http://account-service",
        max_attempts=1,
        backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    await client.apply_transaction(
        EventPayload.model_validate(event_payload),
        trace_id=trace_id,
    )

    assert captured_request is not None
    assert captured_request.headers["X-Trace-Id"] == trace_id
    assert captured_request.headers["traceparent"].startswith(f"00-{trace_id}-")


@pytest.mark.asyncio
async def test_account_client_returns_unavailable_after_bounded_retries(event_payload):
    """Verify retry attempts are bounded when Account Service keeps failing.

    Business view: clients receive a clear unavailable outcome instead of
    hanging indefinitely.
    """

    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return a persistent downstream failure.

        Engineering view: persistent 5xx responses exercise the final failure
        path after the configured retry budget is exhausted.
        """

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


@pytest.mark.asyncio
async def test_account_client_opens_circuit_after_downstream_failures(event_payload):
    """Verify sustained Account Service failures open the client-side circuit.

    Operations view: the breaker protects the Gateway and Account Service from
    repeated calls while a dependency is already known to be failing.
    """

    calls = 0
    current_time = 1000.0

    async def handler(request: httpx.Request) -> httpx.Response:
        """Fail once, then recover after the circuit reset window.

        Engineering view: the fake clock verifies open-window rejection and the
        half-open recovery probe deterministically.
        """

        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"detail": "still down"})
        return httpx.Response(201, json={"ok": True})

    client = HttpAccountClient(
        base_url="http://account-service",
        max_attempts=1,
        backoff_seconds=0,
        circuit_failure_threshold=1,
        circuit_reset_seconds=60,
        clock=lambda: current_time,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AccountServiceUnavailableError):
        await client.apply_transaction(
            EventPayload.model_validate(event_payload),
            trace_id="trace-circuit-123",
        )

    with pytest.raises(AccountServiceCircuitOpenError, match="circuit breaker"):
        await client.apply_transaction(
            EventPayload.model_validate(event_payload),
            trace_id="trace-circuit-123",
        )

    current_time = 1061.0
    result = await client.apply_transaction(
        EventPayload.model_validate(event_payload),
        trace_id="trace-circuit-123",
    )

    assert calls == 2
    assert result == {"ok": True}
