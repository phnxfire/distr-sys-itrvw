"""ASGI application for the internal Account Service.

Engineering view: this module wires the Account Service HTTP boundary to its
repository, observability middleware, and response contracts.
Architecture view: Account Service is internal and owns account state; Gateway
is the only intended caller in the composed runtime.
Business view: this service is responsible for applying transactions exactly
once and reporting account balances accurately.
"""

from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException, Request, Response, status

from account_service.db import (
    AccountCurrencyMismatchError,
    AccountRepository,
    DuplicateEventConflictError,
)
from event_ledger_common.contracts import (
    AccountDetailsResponse,
    BalanceResponse,
    EventPayload,
    HealthResponse,
    TransactionRecord,
)
from event_ledger_common.logging import get_logger
from event_ledger_common.metrics import MetricsRegistry
from event_ledger_common.time import utc_now
from event_ledger_common.trace import (
    TRACE_HEADER,
    TRACEPARENT_HEADER,
    reset_trace_id,
    set_trace_id,
    trace_id_from_headers,
    traceparent_from_trace_id,
)

SERVICE_NAME = "account-service"


def create_app(repository: AccountRepository | None = None) -> FastAPI:
    """Create an Account Service app with injectable persistence for tests.

    Engineering view: injectable persistence makes idempotency and balance tests
    isolated, fast, and deterministic.
    Architecture view: the default repository is service-owned storage, not a
    shared database with Gateway.
    """

    app = FastAPI(title="Event Ledger Account Service", version="0.1.0")
    app.state.repository = repository or AccountRepository(
        os.getenv("ACCOUNT_DB_PATH", "/tmp/event-ledger/account-service.sqlite")
    )
    app.state.metrics = MetricsRegistry()
    app.state.logger = get_logger(SERVICE_NAME)

    @app.middleware("http")
    async def trace_metrics_logging_middleware(request: Request, call_next):
        """Attach trace context, request metrics, and JSON request logging.

        Operations view: Account Service logs can be correlated with Gateway
        logs for a single financial event submission.
        """

        trace_id = trace_id_from_headers(
            request.headers.get(TRACE_HEADER),
            request.headers.get(TRACEPARENT_HEADER),
        )
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            if traceparent := traceparent_from_trace_id(trace_id):
                response.headers[TRACEPARENT_HEADER] = traceparent
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            app.state.metrics.record_request(
                method=request.method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
            )
            app.state.logger.info(
                "request completed",
                extra={
                    "http_method": request.method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 3),
                },
            )
            reset_trace_id(token)

    @app.post(
        "/accounts/{account_id}/transactions",
        response_model=TransactionRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def apply_transaction(
        account_id: str,
        event: EventPayload,
        response: Response,
        request: Request,
    ) -> TransactionRecord:
        """Apply a transaction to one account with idempotent event handling.

        Business view: this is the account-state mutation path. It applies a
        new transaction once, returns exact duplicate replays, and rejects
        conflicting or cross-account requests.
        Architecture view: Gateway can retry this endpoint safely because the
        Account Service protects its own write boundary.
        """

        repository: AccountRepository = request.app.state.repository
        metrics: MetricsRegistry = request.app.state.metrics
        if event.account_id != account_id:
            metrics.record_domain_event("account.transactions.path_mismatch")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path accountId must match payload accountId",
            )
        try:
            record, created = repository.apply_transaction(event)
        except DuplicateEventConflictError as exc:
            metrics.record_domain_event("account.transactions.idempotency_conflict")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="eventId already exists with different transaction details",
            ) from exc
        except AccountCurrencyMismatchError as exc:
            metrics.record_domain_event("account.transactions.currency_conflict")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if not created:
            response.status_code = status.HTTP_200_OK
            metrics.record_domain_event("account.transactions.duplicate_replay")
        else:
            metrics.record_domain_event("account.transactions.applied")
        app.state.logger.info(
            "transaction applied" if created else "duplicate transaction replayed",
            extra={"event_id": event.event_id, "account_id": account_id},
        )
        return record

    @app.get("/accounts/{account_id}/balance", response_model=BalanceResponse)
    async def get_balance(
        account_id: str,
        request: Request,
    ) -> BalanceResponse:
        """Return the Account Service-owned balance for an account.

        Business view: the balance is derived from applied transactions so it
        remains correct even when events arrived out of order.
        """

        repository: AccountRepository = request.app.state.repository
        return repository.get_balance(account_id)

    @app.get("/accounts/{account_id}", response_model=AccountDetailsResponse)
    async def get_account(
        account_id: str,
        request: Request,
    ) -> AccountDetailsResponse:
        """Return account balance and recent chronological transactions.

        Business view: the response explains both the current amount and the
        event-time-ordered transactions behind it.
        """

        repository: AccountRepository = request.app.state.repository
        return repository.get_account_details(account_id)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """Return service status and database connectivity diagnostics.

        Operations view: this supports Docker health checks and quick manual
        verification that account persistence is reachable.
        """

        repository: AccountRepository = request.app.state.repository
        database_status = "ok" if repository.health_check() else "unavailable"
        return HealthResponse(
            service=SERVICE_NAME,
            status="ok" if database_status == "ok" else "degraded",
            database=database_status,
            timestamp=utc_now(),
        )

    @app.get("/metrics")
    async def metrics(request: Request):
        """Return in-process request, latency, error, and domain counters.

        Operations view: reviewers can observe applied transactions, duplicates,
        and conflicts without reading logs or attaching external tooling.
        """

        return request.app.state.metrics.snapshot()

    return app


app = create_app()
