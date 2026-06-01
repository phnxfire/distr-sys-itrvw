# Requirements Traceability

This document maps the take-home requirements to the implementation and tests so reviewers can verify coverage quickly.

## Reviewer Commands

```bash
make install
make lint
make test
docker compose up --build
```

## Requirement Coverage

| Requirement | Implementation | Verification |
| --- | --- | --- |
| Event Gateway accepts transaction events | `POST /events` in `src/gateway_service/main.py`; request contract in `src/event_ledger_common/contracts.py` | `tests/test_gateway_service.py`, `tests/test_integration.py` |
| Gateway validates input | Pydantic `EventPayload` enforces required fields, positive amount, valid type, timezone-aware timestamp, and normalized currency | `test_validation_rejects_bad_event`, `test_validation_rejects_bad_transaction` |
| Gateway stores event records | `GatewayRepository` persists accepted events in its own SQLite database | `test_submit_event_and_duplicate_replay_do_not_reapply`, `test_event_listing_is_chronological_not_arrival_order` |
| Duplicate event replay is idempotent | Gateway returns the original event without recalling Account Service; Account Service also enforces idempotency by `event_id` | `test_submit_event_and_duplicate_replay_do_not_reapply`, `test_duplicate_transaction_does_not_change_balance` |
| Conflicting reuse of an event ID is rejected | Gateway and Account Service compare stored event details and return `409 Conflict` on mismatches | `test_duplicate_event_id_with_different_payload_is_conflict` in both service test files |
| Out-of-order events are tolerated | Event and transaction listings sort by `eventTimestamp`, then `eventId`; balances derive from all applied transactions | `test_event_listing_is_chronological_not_arrival_order`, `test_applies_transactions_and_computes_balance_out_of_order` |
| Balance computation is correct | `AccountRepository.get_balance` computes `sum(CREDIT) - sum(DEBIT)` using `Decimal` values stored as text | `test_applies_transactions_and_computes_balance_out_of_order`, `test_full_gateway_to_account_service_flow` |
| Services are independently runnable | `gateway_service.main` and `account_service.main` define separate FastAPI apps and startup commands | `Makefile`, `docker-compose.yml`, README run instructions |
| Services do not share state | Gateway and Account Service use separate repository classes and separate SQLite DB paths | `src/gateway_service/db.py`, `src/account_service/db.py`, `docker-compose.yml` |
| Clear inter-service contracts | Shared Pydantic models define request and response shapes for both services | `src/event_ledger_common/contracts.py`, integration test |
| Synchronous REST communication | Gateway calls Account Service with HTTPX over REST | `src/gateway_service/account_client.py`, `tests/test_integration.py` |
| Trace ID generation and propagation | Gateway middleware creates or accepts trace context; outbound client forwards `X-Trace-Id` and W3C `traceparent` | `src/event_ledger_common/trace.py`, `test_trace_id_is_propagated_to_account_service`, `test_traceparent_is_accepted_and_echoed` |
| Structured logging with trace IDs | Shared JSON formatter emits timestamp, level, service, message, and trace ID | `src/event_ledger_common/logging.py`, middleware in both services |
| Health checks | Both services expose `/health` with database diagnostics | `test_health_and_metrics` in both service test files |
| Custom metrics | `/metrics` exposes request counts, 5xx counts, latency summaries, and ledger-domain counters | `src/event_ledger_common/metrics.py`, metrics assertions in service tests |
| Resiliency pattern | Gateway Account client implements timeout, bounded retry with exponential backoff, and circuit breaker protection | `test_account_client_retries_server_errors_and_propagates_trace_id`, `test_account_client_opens_circuit_after_downstream_failures` |
| Graceful degradation on Account Service outage | Gateway returns `503` for write/proxy failures while local event reads continue to work | `test_account_service_failure_returns_503_and_event_reads_still_work`, `test_balance_proxy_returns_503_when_account_service_is_down` |
| Docker Compose support | Compose starts both services and publishes only the public Gateway port | `docker-compose.yml`, README Docker instructions |
| Automated tests | Pytest suite covers core behavior, resiliency, tracing, and full Gateway to Account Service flow | `make test`, `tests/` |
| README coverage | README includes architecture, setup, run commands, tests, resiliency choice, observability, and diagrams | `README.md` |

## Explicit Scope Choices

- The system uses synchronous REST because the assignment requires synchronous service communication.
- When Account Service is unavailable, the Gateway rejects new events with `503` instead of accepting pending work. The async fallback queue is documented as a future production upgrade, not implemented.
- OpenTelemetry is represented by W3C `traceparent` propagation and structured trace-aware logs. A collector/exporter stack is documented as a production upgrade.
