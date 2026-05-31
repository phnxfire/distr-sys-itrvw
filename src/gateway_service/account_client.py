"""HTTP client used by Gateway to call the internal Account Service."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from event_ledger_common.contracts import AccountDetailsResponse, BalanceResponse, EventPayload
from event_ledger_common.trace import TRACE_HEADER


class AccountServiceUnavailableError(Exception):
    """Raised when Account Service cannot be reached after bounded retries."""

    pass


class AccountServiceRejectedError(Exception):
    """Raised for non-retriable Account Service validation or contract failures."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class HttpAccountClient:
    """Account Service REST client with timeout, retry, and trace propagation."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 1.5,
        max_attempts: int = 3,
        backoff_seconds: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.transport = transport

    async def apply_transaction(self, event: EventPayload, trace_id: str) -> dict[str, Any]:
        """Apply one transaction through the Account Service transaction endpoint."""

        return await self._request(
            "POST",
            f"/accounts/{event.account_id}/transactions",
            trace_id=trace_id,
            json=event.model_dump(by_alias=True, mode="json"),
        )

    async def get_balance(self, account_id: str, trace_id: str) -> BalanceResponse:
        """Fetch current account balance from Account Service."""

        payload = await self._request(
            "GET",
            f"/accounts/{account_id}/balance",
            trace_id=trace_id,
        )
        return BalanceResponse.model_validate(payload)

    async def get_account(self, account_id: str, trace_id: str) -> AccountDetailsResponse:
        """Fetch account balance and recent transactions from Account Service."""

        payload = await self._request(
            "GET",
            f"/accounts/{account_id}",
            trace_id=trace_id,
        )
        return AccountDetailsResponse.model_validate(payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        trace_id: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a bounded retriable request to Account Service.

        Only connection, timeout, and 5xx failures are retried. 4xx responses are
        treated as deterministic caller or contract errors.
        """

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = await client.request(
                        method,
                        path,
                        json=json,
                        headers={TRACE_HEADER: trace_id},
                    )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.TimeoutException,
            ) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    await asyncio.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise AccountServiceUnavailableError("Account Service is unreachable") from exc

            if response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    "Account Service returned a server error",
                    request=response.request,
                    response=response,
                )
                if attempt < self.max_attempts:
                    # Retrying POST is safe because Account Service enforces
                    # eventId idempotency.
                    await asyncio.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise AccountServiceUnavailableError(
                    "Account Service is unavailable"
                ) from last_error

            if response.status_code >= 400:
                raise AccountServiceRejectedError(response.status_code, _response_detail(response))

            return response.json()

        raise AccountServiceUnavailableError("Account Service is unreachable") from last_error


def _response_detail(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response.text
    return body.get("detail", body)
