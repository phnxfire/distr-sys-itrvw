# Technical Decisions

## ADR 001: Python and FastAPI

**Decision:** Use Python with FastAPI.

**Why:** FastAPI supports concise REST APIs, Pydantic validation, async HTTP clients, and fast local testing. It keeps the implementation small enough for a 3-4 hour exercise while still demonstrating realistic service boundaries.

**Tradeoff:** A Java/Spring Boot implementation might look more enterprise-standard, but would add boilerplate without improving the core distributed-systems signal for this exercise.

## ADR 002: SQLite Per Service

**Decision:** Use one SQLite database per service.

**Why:** SQLite satisfies the embedded database constraint, provides real uniqueness constraints and indexes, and runs locally without external infrastructure. Each service owns its own DB file, preserving the no-shared-state requirement.

**Tradeoff:** SQLite is not intended to represent production multi-node storage. For this exercise, the important behavior is service-owned persistence and constraint-backed idempotency.

## ADR 003: Idempotency at Both Boundaries

**Decision:** Enforce unique `eventId` in both Gateway and Account Service.

**Why:** Gateway idempotency prevents duplicate public event records and avoids unnecessary internal calls. Account Service idempotency protects the balance if the Gateway retries after a timeout or if requests race.

**Tradeoff:** This duplicates a small amount of logic, but the duplication is intentional defense in depth around money movement.

## ADR 004: Timeout, Retry, and Circuit Breaker

**Decision:** Implement timeout, retry with exponential backoff, and a circuit breaker for Gateway to Account Service calls.

**Why:** The exercise requires at least one resiliency pattern. Timeout plus retry directly addresses transient failures, while the circuit breaker protects the Account Service and Gateway worker capacity during sustained failure. The retry policy is bounded to avoid unbounded client hangs.

**Tradeoff:** The circuit breaker is in process and per Gateway instance. A production deployment would externalize visibility through metrics and tune thresholds per environment, but the behavior is enough to demonstrate the pattern safely because Account Service calls are idempotent.

## ADR 005: Lightweight Trace Propagation With W3C Compatibility

**Decision:** Use `X-Trace-Id` plus W3C `traceparent` propagation instead of a full OpenTelemetry collector.

**Why:** The requirement says OpenTelemetry is preferred, not required. `X-Trace-Id` keeps local debugging simple, while `traceparent` models the professional W3C propagation format used by OpenTelemetry. One client request can be correlated across service logs and is ready for later span export.

**Tradeoff:** This does not provide span timing visualization or trace sampling. The code is structured so OpenTelemetry middleware/exporters could be added later without changing public API behavior.

## ADR 006: Store Only Applied Events in the Gateway

**Decision:** The Gateway stores events after the Account Service has accepted or replayed the transaction.

**Why:** This keeps Gateway event history aligned with account state for this synchronous design. If the Account Service is unavailable, the request fails with `503` and the transaction is not accepted.

**Tradeoff:** This does not implement the bonus async fallback queue. A production ledger might persist a pending event and reconcile it through a durable outbox or workflow engine.

## ADR 007: Single Currency Per Account

**Decision:** The Account Service rejects transactions that introduce a second currency for an existing account.

**Why:** The exercise defines a single net balance formula and does not specify multi-currency accounting. Enforcing a single account currency prevents accidentally summing incompatible monetary units.

**Tradeoff:** A real financial platform would model ledgers by currency or account sub-ledger.

## Principles and Patterns Used

- **Single Responsibility:** Gateway owns public API and event history; Account Service owns account state.
- **Database Per Service:** each service has independent SQLite storage.
- **Idempotent Consumer:** duplicate `eventId` requests do not mutate state more than once.
- **Defensive Idempotency:** both services guard their own write boundary.
- **Fail Fast:** downstream timeouts are bounded and converted into `503` responses.
- **Circuit Breaker:** repeated Account Service availability failures are short-circuited for a reset window.
- **Explicit Contracts:** shared Pydantic models define request and response shapes.
- **Structured Observability:** logs, metrics, health checks, and trace IDs are first-class behavior.
- **Deterministic Ordering:** event histories sort by event time, not arrival time.
