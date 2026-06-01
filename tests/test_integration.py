"""End-to-end tests across the Gateway and Account Service ASGI applications.

Architecture view: this module proves the two service boundaries work together
over HTTP-style transports while still using isolated embedded databases.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from account_service.db import AccountRepository
from account_service.main import create_app as create_account_app
from gateway_service.account_client import HttpAccountClient
from gateway_service.db import GatewayRepository
from gateway_service.main import create_app as create_gateway_app


@pytest.mark.asyncio
async def test_full_gateway_to_account_service_flow(tmp_path, event_payload, debit_payload):
    """Exercise the full Gateway to Account Service transaction flow.

    Business view: this test shows the complete path from public event intake to
    account balance and event-history reads.
    """

    account_app = create_account_app(
        repository=AccountRepository(tmp_path / "account-integration.sqlite")
    )
    account_transport = ASGITransport(app=account_app)
    account_client = HttpAccountClient(
        base_url="http://account-service",
        max_attempts=2,
        backoff_seconds=0,
        transport=account_transport,
    )
    gateway_app = create_gateway_app(
        repository=GatewayRepository(tmp_path / "gateway-integration.sqlite"),
        account_client=account_client,
    )

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://gateway",
    ) as client:
        credit = await client.post(
            "/events",
            json=event_payload,
            headers={"X-Trace-Id": "trace-integration-123"},
        )
        debit = await client.post("/events", json=debit_payload)
        balance = await client.get("/accounts/acct-123/balance")
        events = await client.get("/events", params={"account": "acct-123"})

    assert credit.status_code == 201
    assert credit.headers["X-Trace-Id"] == "trace-integration-123"
    assert debit.status_code == 201
    assert balance.status_code == 200
    assert balance.json() == {
        "accountId": "acct-123",
        "balance": 124.5,
        "currency": "USD",
    }
    assert [event["eventId"] for event in events.json()] == ["evt-001", "evt-002"]
