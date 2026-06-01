"""HTTP client used by Gateway to call the internal Account Service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import Any

import httpx

from event_ledger_common.contracts import AccountDetailsResponse, BalanceResponse, EventPayload
from event_ledger_common.trace import TRACE_HEADER, TRACEPARENT_HEADER, traceparent_from_trace_id


class AccountServiceUnavailableError(Exception):
    """Raised when Account Service cannot be reached after bounded retries."""

    pass


class AccountServiceCircuitOpenError(AccountServiceUnavailableError):
    """Raised when the client-side circuit breaker rejects a downstream call."""

    pass


class AccountServiceRejectedError(Exception):
    """Raised for non-retriable Account Service validation or contract failures."""

    def __init__(self, status_code: int, detail: Any) -> None:
        """Capture the downstream HTTP status and response detail."""

        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class HttpAccountClient:
    """Account Service REST client with timeout, retry, circuit breaker, and tracing.

    Gateway owns the public write boundary, so its downstream client has to be
    conservative: it retries only transient failures, never retries deterministic
    4xx contract errors, and opens a circuit after repeated availability
    failures to avoid amplifying an Account Service outage.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 1.5,
        max_attempts: int = 3,
        backoff_seconds: float = 0.1,
        circuit_failure_threshold: int = 3,
        circuit_reset_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Configure Account Service connectivity, retry, and circuit breaker policy."""

        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.circuit_failure_threshold = max(1, circuit_failure_threshold)
        self.circuit_reset_seconds = max(0.0, circuit_reset_seconds)
        self._clock = clock or monotonic
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None
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
        treated as deterministic caller or contract errors. The circuit breaker
        opens only after all retry attempts fail for availability reasons.
        """

        self._ensure_circuit_allows_request()
        last_error: Exception | None = None
        headers = {TRACE_HEADER: trace_id}
        if traceparent := traceparent_from_trace_id(trace_id):
            headers[TRACEPARENT_HEADER] = traceparent

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
                        headers=headers,
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
                self._record_downstream_failure()
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
                self._record_downstream_failure()
                raise AccountServiceUnavailableError(
                    "Account Service is unavailable"
                ) from last_error

            if response.status_code >= 400:
                # A 4xx response is a contract/business rejection, not an
                # availability failure, so it should not keep the circuit open.
                self._record_downstream_success()
                raise AccountServiceRejectedError(response.status_code, _response_detail(response))

            self._record_downstream_success()
            return response.json()

        self._record_downstream_failure()
        raise AccountServiceUnavailableError("Account Service is unreachable") from last_error

    def _ensure_circuit_allows_request(self) -> None:
        """Reject calls while the Account Service circuit is still open.

        After the reset window expires, the next request is allowed through as a
        lightweight half-open probe. A success closes the circuit; a failure
        opens it again.
        """

        if self._circuit_opened_at is None:
            return
        elapsed_seconds = self._clock() - self._circuit_opened_at
        if elapsed_seconds >= self.circuit_reset_seconds:
            return
        raise AccountServiceCircuitOpenError("Account Service circuit breaker is open")

    def _record_downstream_success(self) -> None:
        """Close the circuit and clear failure state after a successful response."""

        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_downstream_failure(self) -> None:
        """Track availability failures and open the circuit at the threshold."""

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.circuit_failure_threshold:
            self._circuit_opened_at = self._clock()


def _response_detail(response: httpx.Response) -> Any:
    """Extract a useful error detail from an HTTPX response."""

    try:
        body = response.json()
    except ValueError:
        return response.text
    return body.get("detail", body)
