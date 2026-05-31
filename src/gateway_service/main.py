"""ASGI application for the public Event Gateway API."""

from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException, Query, Request, Response, status

from event_ledger_common.contracts import (
    AccountDetailsResponse,
    BalanceResponse,
    EventPayload,
    EventRecord,
    HealthResponse,
)
from event_ledger_common.logging import get_logger
from event_ledger_common.metrics import MetricsRegistry
from event_ledger_common.time import utc_now
from event_ledger_common.trace import (
    TRACE_HEADER,
    get_trace_id,
    reset_trace_id,
    set_trace_id,
    trace_id_from_header,
)
from gateway_service.account_client import (
    AccountServiceRejectedError,
    AccountServiceUnavailableError,
    HttpAccountClient,
)
from gateway_service.db import DuplicateEventConflictError, GatewayRepository

SERVICE_NAME = "event-gateway"


def create_app(
    *,
    repository: GatewayRepository | None = None,
    account_client: HttpAccountClient | None = None,
) -> FastAPI:
    """Create a Gateway app with injectable repository and Account client."""

    app = FastAPI(title="Event Ledger Gateway API", version="0.1.0")
    app.state.repository = repository or GatewayRepository(
        os.getenv("GATEWAY_DB_PATH", "/tmp/event-ledger/gateway.sqlite")
    )
    app.state.account_client = account_client or HttpAccountClient(
        base_url=os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8001"),
        timeout_seconds=float(os.getenv("ACCOUNT_SERVICE_TIMEOUT_SECONDS", "1.5")),
        max_attempts=int(os.getenv("ACCOUNT_SERVICE_MAX_ATTEMPTS", "3")),
        backoff_seconds=float(os.getenv("ACCOUNT_SERVICE_BACKOFF_SECONDS", "0.1")),
    )
    app.state.metrics = MetricsRegistry()
    app.state.logger = get_logger(SERVICE_NAME)

    @app.middleware("http")
    async def trace_metrics_logging_middleware(request: Request, call_next):
        """Create/propagate trace IDs and record request observability data."""

        trace_id = trace_id_from_header(request.headers.get(TRACE_HEADER))
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
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

    @app.post("/events", response_model=EventRecord, status_code=status.HTTP_201_CREATED)
    async def submit_event(
        event: EventPayload,
        response: Response,
        request: Request,
    ) -> EventRecord:
        """Accept a transaction event after downstream account application."""

        repository: GatewayRepository = request.app.state.repository
        account_service: HttpAccountClient = request.app.state.account_client
        existing = repository.get_event(event.event_id)
        if existing:
            if not _same_event(existing, event):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="eventId already exists with different event details",
                )
            response.status_code = status.HTTP_200_OK
            app.state.logger.info(
                "duplicate event replayed",
                extra={"event_id": event.event_id, "account_id": event.account_id},
            )
            return existing

        try:
            await account_service.apply_transaction(event, trace_id=get_trace_id())
        except AccountServiceUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unavailable; transaction was not accepted",
            ) from exc
        except AccountServiceRejectedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        try:
            record, created = repository.insert_applied_event(event)
        except DuplicateEventConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="eventId already exists with different event details",
            ) from exc

        if not created:
            response.status_code = status.HTTP_200_OK

        app.state.logger.info(
            "event accepted",
            extra={"event_id": event.event_id, "account_id": event.account_id},
        )
        return record

    @app.get("/events/{event_id}", response_model=EventRecord)
    async def get_event(
        event_id: str,
        request: Request,
    ) -> EventRecord:
        """Return one Gateway-owned event record by eventId."""

        repository: GatewayRepository = request.app.state.repository
        event = repository.get_event(event_id)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
        return event

    @app.get("/events", response_model=list[EventRecord])
    async def list_events(
        request: Request,
        account: str = Query(..., min_length=1),
    ) -> list[EventRecord]:
        """Return Gateway-owned events for an account in chronological order."""

        repository: GatewayRepository = request.app.state.repository
        return repository.list_events_for_account(account)

    @app.get("/accounts/{account_id}/balance", response_model=BalanceResponse)
    async def get_balance(
        account_id: str,
        request: Request,
    ) -> BalanceResponse:
        """Proxy account balance reads to the Account Service."""

        account_service: HttpAccountClient = request.app.state.account_client
        try:
            return await account_service.get_balance(account_id, trace_id=get_trace_id())
        except AccountServiceUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unavailable; balance cannot be retrieved",
            ) from exc
        except AccountServiceRejectedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/accounts/{account_id}", response_model=AccountDetailsResponse)
    async def get_account(
        account_id: str,
        request: Request,
    ) -> AccountDetailsResponse:
        """Proxy account detail reads to the Account Service."""

        account_service: HttpAccountClient = request.app.state.account_client
        try:
            return await account_service.get_account(account_id, trace_id=get_trace_id())
        except AccountServiceUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unavailable; account details cannot be retrieved",
            ) from exc
        except AccountServiceRejectedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """Return Gateway status and local database diagnostics."""

        repository: GatewayRepository = request.app.state.repository
        database_status = "ok" if repository.health_check() else "unavailable"
        return HealthResponse(
            service=SERVICE_NAME,
            status="ok" if database_status == "ok" else "degraded",
            database=database_status,
            timestamp=utc_now(),
        )

    @app.get("/metrics")
    async def metrics(request: Request):
        """Return in-process request counters and latency summaries."""

        return request.app.state.metrics.snapshot()

    return app


def _same_event(existing: EventPayload, incoming: EventPayload) -> bool:
    """Return whether two event payloads are equivalent for idempotency."""

    return (
        existing.event_id == incoming.event_id
        and existing.account_id == incoming.account_id
        and existing.type == incoming.type
        and existing.amount == incoming.amount
        and existing.currency == incoming.currency
        and existing.event_timestamp == incoming.event_timestamp
        and existing.metadata == incoming.metadata
    )


app = create_app()
