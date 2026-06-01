# Delivery Readiness Review

This review maps the take-home evaluation goals to the implementation and identifies the professional upgrade path for production-grade operation.

## Executive Summary

The project demonstrates a bounded distributed financial event processor with two independently runnable services:

- Event Gateway API: public API, validation, event idempotency, trace boundary, local event history, Account Service client.
- Account Service: internal transaction application, balance calculation, account details, transaction history.

The implementation intentionally keeps infrastructure lightweight while preserving the important distributed-systems properties: database-per-service, idempotent writes, out-of-order event tolerance, bounded downstream retries, a circuit breaker, structured logs, request and domain metrics, health checks, and trace propagation.

## Evaluation Mapping

| Evaluation Area | Current Implementation | Professional Next Step |
| --- | --- | --- |
| Build and operate distributed systems | Two independently runnable FastAPI services with separate SQLite databases and REST contracts. | Add CI/CD, deployment manifests, dashboards, alerts, and load testing. |
| Process financial transaction events | Validates event payloads, applies CREDIT/DEBIT transactions, computes balances from applied transactions. | Add ledger accounting controls, audit export, reconciliation jobs, and immutable append-only storage. |
| Multiple upstream systems | Event IDs and timestamps are source-agnostic; metadata can carry source/batch information. | Add upstream authentication, source-level rate limits, schema versioning, and contract tests per producer. |
| Duplicate delivery | Unique `eventId` constraints in both Gateway and Account Service. | Add dedicated idempotency tables with request hash, response replay, TTL/retention policy, and conflict audit logs. |
| Out-of-order delivery | Event and transaction history sort by original `eventTimestamp`; balances derive from all applied transactions. | Add correction/reversal events, business-effective dating rules, and late-arrival monitoring. |
| Observability | JSON logs, trace IDs, W3C `traceparent` support, request/domain metrics endpoint, health checks. | Add OpenTelemetry SDK/exporters, Prometheus metrics, dashboards, log correlation, and alerting. |
| Resiliency | Gateway uses timeout, bounded retry with exponential backoff, and a circuit breaker on Account Service calls. | Add bulkheads, retry budgets, connection-pool limits, and downstream saturation metrics. |
| Operational readiness | Docker Compose, health checks, tests, docs, rendered architecture diagrams. | Add runbooks, SLOs, incident playbooks, migrations, and environment-specific config validation. |
| Engineering decisions | ADR-style docs explain FastAPI, SQLite, idempotency, tracing, and retry choices. | Add explicit threat model, scalability model, and data retention policy. |

## Gateway Transaction Flow

The Gateway handles `POST /events` as the public consistency boundary:

1. Validates payload using Pydantic contracts.
2. Checks local Gateway storage for an existing `eventId`.
3. Returns `200 OK` for exact duplicate replays without calling Account Service.
4. Returns `409 Conflict` when an existing `eventId` is reused with different details.
5. Calls Account Service for new events using HTTP with timeout, retry, circuit breaker protection, and trace propagation.
6. Stores the Gateway event record only after Account Service applies or idempotently replays the transaction.
7. Returns `201 Created` for newly accepted events.

This design keeps Gateway event reads aligned with account state for the synchronous REST version. If the Account Service is unavailable, `POST /events` returns `503 Service Unavailable` and does not accept the transaction.

## Account State Management

The Account Service owns account state:

- transactions are stored in the Account Service SQLite database
- `event_id` is the Account Service idempotency key
- balances are calculated as `sum(CREDIT) - sum(DEBIT)`
- account details include a bounded chronological transaction history
- a single account currency is enforced to avoid summing incompatible monetary units

In Docker Compose, only the Gateway publishes a host port. Account Service is available to Gateway on the internal Compose network and is not exposed as a public client API.

## Professional Idempotency Approach

The current implementation uses the right core pattern: idempotent consumer with a unique idempotency key at every write boundary.

For a production financial system, the professional version would usually include:

- idempotency key table separate from the business table
- request payload hash to detect conflicting retries
- stored response body/status for exact response replay
- idempotency retention policy
- audit log for conflicts
- safe retry semantics documented in the API contract
- database uniqueness constraints as the final guardrail

The take-home version implements the essential behavior with `eventId` uniqueness and full payload comparison in both services.

## Professional Out-of-Order Approach

The current implementation treats arrival time and event time as different concepts. It stores original `eventTimestamp` and sorts event history by that business timestamp.

For production, the next level would include:

- event-time versus processing-time metrics
- late-arrival thresholds and alerts
- explicit reversal/correction event types
- business rules for backdated events
- immutable ledger entries instead of mutable balance snapshots
- reconciliation reports between source systems and applied ledger entries

The take-home version computes balance from the full applied transaction set, so arrival order cannot change the result.

## OpenTelemetry Simulation and Upgrade Path

The project now supports two trace propagation styles:

- `X-Trace-Id`: simple human-readable correlation for local debugging
- W3C `traceparent`: professional distributed tracing header compatible with OpenTelemetry concepts

Both services:

- accept incoming `traceparent`
- extract the W3C trace ID
- set the active request trace context
- echo `X-Trace-Id` and `traceparent` in responses
- include trace IDs in structured logs

Gateway outbound Account Service calls include both `X-Trace-Id` and `traceparent` when the trace ID is W3C-compatible.

The production upgrade is straightforward:

- add OpenTelemetry FastAPI instrumentation
- add HTTPX client instrumentation
- export spans to an OpenTelemetry Collector
- visualize traces in Jaeger, Tempo, Honeycomb, Datadog, or another backend
- preserve the same W3C trace context behavior already represented here

## Observability Improvement Plan

The current observability is useful for local operation and review:

- structured JSON request logs
- trace correlation across services
- `/health` database diagnostics
- `/metrics` request counts, 5xx counts, latency summaries, and domain counters
- clear `503` responses during Account Service outages

Recommended production improvements:

1. Replace in-memory metrics with Prometheus counters and histograms.
2. Add RED metrics: request rate, error rate, duration.
3. Expand ledger-specific metrics with retry attempts, circuit breaker state gauges, late-arriving event count, and source-system labels.
4. Add structured audit events for accepted, replayed, rejected, and failed transactions.
5. Add alerting for elevated 5xx rate, retry exhaustion, latency, and database health failures.
6. Add dashboards for Gateway traffic, Account Service traffic, downstream retries, and transaction acceptance.
7. Add log sampling or retention policies for high-volume environments.

## Resiliency Improvement Plan

Current resiliency is timeout, retry with exponential backoff, and a circuit breaker. This is appropriate for transient Account Service failures, protects the dependency during sustained failure, and is safe because Account Service enforces idempotency.

Recommended production improvements:

1. Add retry budgets to prevent retry storms.
2. Add bulkhead isolation for outbound Account Service calls.
3. Add connection pooling and explicit pool limits.
4. Export circuit breaker state gauges and open-count metrics to the metrics backend.
5. Add an outbox pattern if the product requirement changes from synchronous rejection to asynchronous acceptance.
6. Add reconciliation to detect Gateway/Account divergence after process crashes.

## Known Tradeoff

The main distributed-systems tradeoff is the synchronous boundary between Gateway and Account Service. The current design returns `503` and does not accept new events when Account Service is unavailable. This is honest and simple for the assignment.

If the business required accepting events during Account Service outages, the architecture should add a durable outbox or queue with asynchronous processing, replay, reconciliation, and explicit `PENDING`/`APPLIED` states.

## Final Delivery Posture

This solution is ready as a take-home submission because it demonstrates the requested engineering signals:

- separated services
- independent databases
- correct idempotency behavior
- out-of-order tolerance
- graceful degradation
- trace propagation
- structured logs
- health, request metrics, and domain metrics endpoints
- automated tests
- architecture documentation and diagrams

The README and ADRs clearly explain which decisions are scoped for the exercise and which would be upgraded for production.
