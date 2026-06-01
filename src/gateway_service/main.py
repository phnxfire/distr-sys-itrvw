"""ASGI application for the public Event Gateway API.

Engineering view: this module wires FastAPI routes, middleware, persistence,
downstream HTTP calls, metrics, and structured logging into one runnable app.
Architecture view: Gateway is the public boundary; it validates events,
enforces edge idempotency, owns public event history, and delegates account
state to Account Service.
Business view: Gateway decides whether a submitted financial event is accepted,
replayed, rejected, or unavailable.
"""

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
    TRACEPARENT_HEADER,
    get_trace_id,
    reset_trace_id,
    set_trace_id,
    trace_id_from_headers,
    traceparent_from_trace_id,
)
from gateway_service.account_client import (
    AccountServiceCircuitOpenError,
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
    """Create a Gateway app with injectable repository and Account client.

    Engineering view: dependency injection keeps route logic testable without
    starting real network services or sharing state between tests.
    Architecture view: production uses environment-configured dependencies,
    while tests can supply in-memory repositories and fake clients.
    """

    app = FastAPI(title="Event Ledger Gateway API", version="0.1.0")
    app.state.repository = repository or GatewayRepository(
        os.getenv("GATEWAY_DB_PATH", "/tmp/event-ledger/gateway.sqlite")
    )
    app.state.account_client = account_client or HttpAccountClient(
        base_url=os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8001"),
        timeout_seconds=float(os.getenv("ACCOUNT_SERVICE_TIMEOUT_SECONDS", "1.5")),
        max_attempts=int(os.getenv("ACCOUNT_SERVICE_MAX_ATTEMPTS", "3")),
        backoff_seconds=float(os.getenv("ACCOUNT_SERVICE_BACKOFF_SECONDS", "0.1")),
        circuit_failure_threshold=int(
            os.getenv("ACCOUNT_SERVICE_CIRCUIT_FAILURE_THRESHOLD", "3")
        ),
        circuit_reset_seconds=float(os.getenv("ACCOUNT_SERVICE_CIRCUIT_RESET_SECONDS", "5.0")),
    )
    app.state.metrics = MetricsRegistry()
    app.state.logger = get_logger(SERVICE_NAME)

    @app.middleware("http")
    async def trace_metrics_logging_middleware(request: Request, call_next):
        """Create/propagate trace IDs and record request observability data.

        Operations view: every request receives correlation headers, latency
        metrics, and structured logs so a reviewer can trace Gateway behavior.
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

    @app.post("/events", response_model=EventRecord, status_code=status.HTTP_201_CREATED)
    async def submit_event(
        event: EventPayload,
        response: Response,
        request: Request,
    ) -> EventRecord:
        """Accept a transaction event after downstream account application.

        Business view: this is the public write path. It accepts exact duplicate
        replays, rejects conflicting idempotency keys, and records only events
        that Account Service has applied or idempotently replayed.
        Architecture view: Gateway does not compute balances; it coordinates
        with Account Service and persists the public event record.
        """

        repository: GatewayRepository = request.app.state.repository
        account_service: HttpAccountClient = request.app.state.account_client
        metrics: MetricsRegistry = request.app.state.metrics
        existing = repository.get_event(event.event_id)
        if existing:
            if not _same_event(existing, event):
                metrics.record_domain_event("gateway.events.idempotency_conflict")
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="eventId already exists with different event details",
                )
            response.status_code = status.HTTP_200_OK
            metrics.record_domain_event("gateway.events.duplicate_replay")
            app.state.logger.info(
                "duplicate event replayed",
                extra={"event_id": event.event_id, "account_id": event.account_id},
            )
            return existing

        try:
            await account_service.apply_transaction(event, trace_id=get_trace_id())
        except AccountServiceCircuitOpenError as exc:
            metrics.record_domain_event("gateway.account_service.circuit_open")
            metrics.record_domain_event("gateway.account_service.unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service circuit breaker is open; transaction was not accepted",
            ) from exc
        except AccountServiceUnavailableError as exc:
            metrics.record_domain_event("gateway.account_service.unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unavailable; transaction was not accepted",
            ) from exc
        except AccountServiceRejectedError as exc:
            metrics.record_domain_event("gateway.account_service.rejected")
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        try:
            record, created = repository.insert_applied_event(event)
        except DuplicateEventConflictError as exc:
            metrics.record_domain_event("gateway.events.idempotency_conflict")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="eventId already exists with different event details",
            ) from exc

        if not created:
            response.status_code = status.HTTP_200_OK
            metrics.record_domain_event("gateway.events.duplicate_replay")
        else:
            metrics.record_domain_event("gateway.events.accepted")

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
        """Return one Gateway-owned event record by eventId.

        Graceful degradation view: this read depends only on Gateway storage, so
        it still works when Account Service is unavailable.
        """

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
        """Return Gateway-owned events for an account in chronological order.

        Business view: clients see event history ordered by original event time,
        not by whichever upstream system delivered first.
        """

        repository: GatewayRepository = request.app.state.repository
        return repository.list_events_for_account(account)

    @app.get("/accounts/{account_id}/balance", response_model=BalanceResponse)
    async def get_balance(
        account_id: str,
        request: Request,
    ) -> BalanceResponse:
        """Proxy account balance reads to the Account Service.

        Architecture view: Gateway exposes a convenient public read endpoint
        while preserving Account Service ownership of balances.
        """

        account_service: HttpAccountClient = request.app.state.account_client
        metrics: MetricsRegistry = request.app.state.metrics
        try:
            return await account_service.get_balance(account_id, trace_id=get_trace_id())
        except AccountServiceCircuitOpenError as exc:
            metrics.record_domain_event("gateway.account_service.circuit_open")
            metrics.record_domain_event("gateway.account_service.unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service circuit breaker is open; balance cannot be retrieved",
            ) from exc
        except AccountServiceUnavailableError as exc:
            metrics.record_domain_event("gateway.account_service.unavailable")
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
        """Proxy account detail reads to the Account Service.

        Business view: account details come from the service that owns applied
        transactions, keeping the response authoritative.
        """

        account_service: HttpAccountClient = request.app.state.account_client
        metrics: MetricsRegistry = request.app.state.metrics
        try:
            return await account_service.get_account(account_id, trace_id=get_trace_id())
        except AccountServiceCircuitOpenError as exc:
            metrics.record_domain_event("gateway.account_service.circuit_open")
            metrics.record_domain_event("gateway.account_service.unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Account Service circuit breaker is open; "
                    "account details cannot be retrieved"
                ),
            ) from exc
        except AccountServiceUnavailableError as exc:
            metrics.record_domain_event("gateway.account_service.unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Account Service is unavailable; account details cannot be retrieved",
            ) from exc
        except AccountServiceRejectedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """Return Gateway status and local database diagnostics.

        Operations view: this is the lightweight readiness signal used by
        humans, tests, and Docker Compose health checks.
        """

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
        """Return in-process request, latency, error, and domain counters.

        Operations view: exposing metrics through HTTP makes the take-home easy
        to inspect without adding monitoring infrastructure.
        """

        return request.app.state.metrics.snapshot()

    return app


def _same_event(existing: EventPayload, incoming: EventPayload) -> bool:
    """Return whether two event payloads are equivalent for idempotency.

    Business view: exact event replays are accepted, but any changed financial
    fact under the same eventId is treated as a conflict.
    """

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
