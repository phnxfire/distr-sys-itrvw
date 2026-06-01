# Event Ledger

[![CI](https://github.com/phnxfire/distr-sys-itrvw/actions/workflows/ci.yml/badge.svg)](https://github.com/phnxfire/distr-sys-itrvw/actions/workflows/ci.yml)

Event Ledger is a two-service take-home project for processing financial transaction events from upstream systems that can deliver duplicates and out-of-order events.

The solution focuses on correctness, service separation, observability, and graceful failure:

- idempotent `eventId` handling in both services
- chronological event history regardless of arrival order
- balance computation from applied account transactions
- independent SQLite databases per service
- structured JSON logs with propagated trace IDs
- health and metrics endpoints
- timeout, retry, exponential backoff, and a circuit breaker for Gateway to Account Service calls

## Evaluator Quick Start

Validate the project locally:

```bash
make install
make lint
make test
```

Run both services with Docker Compose:

```bash
docker compose up --build
```

Submit a sample event through the public Gateway:

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{
    "eventId": "evt-001",
    "accountId": "acct-123",
    "type": "CREDIT",
    "amount": 150.0,
    "currency": "USD",
    "eventTimestamp": "2026-05-15T14:02:11Z",
    "metadata": {"source": "mainframe-batch"}
  }'
```

For requirement-by-requirement coverage, see [Requirements Traceability](docs/requirements-traceability.md).

## Architecture

The system has two independently runnable FastAPI services.

| Service | Responsibility | Local State |
| --- | --- | --- |
| Event Gateway API | Public API, validation, event idempotency, event history, trace generation, Account Service calls | SQLite via `GATEWAY_DB_PATH`, default `/tmp/event-ledger/gateway.sqlite` |
| Account Service | Internal account transaction application, account balance, account details | SQLite via `ACCOUNT_DB_PATH`, default `/tmp/event-ledger/account-service.sqlite` |

The Gateway and Account Service do not share database connections, tables, or in-process state. They communicate synchronously over REST.

See [docs/architecture.md](docs/architecture.md) for C4 diagrams and request-flow details.
See [docs/delivery-readiness-review.md](docs/delivery-readiness-review.md) for the delivery-readiness review, operational tradeoffs, and production improvement plan.

Rendered diagram images are also available as SVG files:

- [System Context](docs/diagrams/c4-context.svg)
- [Container Diagram](docs/diagrams/c4-container.svg)
- [Gateway Component Diagram](docs/diagrams/c4-component-gateway.svg)
- [Account Service Component Diagram](docs/diagrams/c4-component-account-service.svg)

## API

### Gateway

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/events` | Submit a transaction event |
| `GET` | `/events/{eventId}` | Retrieve a single event |
| `GET` | `/events?account={accountId}` | List events for an account ordered by `eventTimestamp` |
| `GET` | `/accounts/{accountId}/balance` | Proxy balance query to Account Service |
| `GET` | `/accounts/{accountId}` | Proxy account details query to Account Service |
| `GET` | `/health` | Gateway health check |
| `GET` | `/metrics` | JSON request/error/latency/domain metrics |

### Account Service

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/accounts/{accountId}/transactions` | Apply a transaction to an account |
| `GET` | `/accounts/{accountId}/balance` | Get current account balance |
| `GET` | `/accounts/{accountId}` | Get balance and recent transactions |
| `GET` | `/health` | Account Service health check |
| `GET` | `/metrics` | JSON request/error/latency/domain metrics |

## Event Payload

```json
{
  "eventId": "evt-001",
  "accountId": "acct-123",
  "type": "CREDIT",
  "amount": 150.0,
  "currency": "USD",
  "eventTimestamp": "2026-05-15T14:02:11Z",
  "metadata": {
    "source": "mainframe-batch",
    "batchId": "B-9042"
  }
}
```

## Running Locally

Install dependencies:

```bash
make install
```

Run the Account Service:

```bash
make run-account
```

Run the Gateway in another terminal:

```bash
make run-gateway
```

The Gateway listens on `http://localhost:8000`. The Account Service listens on `http://localhost:8001`.

## Running With Docker Compose

```bash
docker compose up --build
```

Then call the Gateway on `http://localhost:8000`. The Account Service is intentionally not published to the host in Docker Compose; Gateway reaches it through the internal Compose network.

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{
    "eventId": "evt-001",
    "accountId": "acct-123",
    "type": "CREDIT",
    "amount": 150.0,
    "currency": "USD",
    "eventTimestamp": "2026-05-15T14:02:11Z",
    "metadata": {"source": "mainframe-batch"}
  }'
```

## Configuration

| Variable | Default | Used By | Purpose |
| --- | --- | --- | --- |
| `GATEWAY_DB_PATH` | `/tmp/event-ledger/gateway.sqlite` | Gateway | Gateway event-store database path |
| `ACCOUNT_DB_PATH` | `/tmp/event-ledger/account-service.sqlite` | Account Service | Account transaction database path |
| `ACCOUNT_SERVICE_URL` | `http://localhost:8001` | Gateway | Internal Account Service base URL |
| `ACCOUNT_SERVICE_TIMEOUT_SECONDS` | `1.5` | Gateway | Per-attempt downstream timeout |
| `ACCOUNT_SERVICE_MAX_ATTEMPTS` | `3` | Gateway | Maximum attempts before returning `503` |
| `ACCOUNT_SERVICE_BACKOFF_SECONDS` | `0.1` | Gateway | Base exponential backoff delay |
| `ACCOUNT_SERVICE_CIRCUIT_FAILURE_THRESHOLD` | `3` | Gateway | Availability failures before opening the circuit |
| `ACCOUNT_SERVICE_CIRCUIT_RESET_SECONDS` | `5.0` | Gateway | Open-circuit reset window before the next probe |

## Tests

Run the automated test suite:

```bash
make test
```

The tests cover validation, idempotency, out-of-order event listing, balance correctness, Account Service failure handling, retry and circuit breaker behavior, domain metrics, trace propagation, and an end-to-end Gateway to Account Service flow.

## Postman

Import [docs/postman/Event-Ledger.postman_collection.json](docs/postman/Event-Ledger.postman_collection.json) into Postman to run an evaluator-friendly request sequence against a local server.

The collection covers:

- Gateway and Account Service health checks
- successful credit submission
- exact duplicate replay
- conflicting duplicate rejection
- out-of-order debit submission
- chronological event listing
- balance and account detail reads
- Gateway domain metrics

Run the services first with either `docker compose up --build` or the local `make run-account` and `make run-gateway` commands. The collection defaults to:

- Gateway: `http://127.0.0.1:8000`
- Account Service: `http://127.0.0.1:8001`

## Diagrams

The Mermaid C4 source files live under `docs/diagrams/*.mmd`. Rendered SVG images can be regenerated with:

```bash
npm install
npm run render:diagrams
```

## Resiliency Choice

The Gateway uses **timeout, retry with exponential backoff, and a circuit breaker** for Account Service calls.

This is the best fit for a small synchronous REST system because many Account Service failures are transient: startup race, brief network failure, or short overload. The implementation bounds the blast radius with:

- a short per-request timeout
- a fixed maximum attempt count
- exponential backoff between attempts
- a circuit breaker that short-circuits calls during sustained downstream failure
- `503 Service Unavailable` responses when the Account Service remains unreachable

Retries are safe because the Account Service also enforces idempotency on `eventId`.

## Observability

Both services expose:

- `GET /health` for status and database diagnostics
- `GET /metrics` for request counts, error counts, latency summaries, and domain event counters
- JSON structured logs with `timestamp`, `level`, `service`, `trace_id`, and HTTP details

Trace IDs are generated at the Gateway when absent, accepted from `X-Trace-Id` or W3C `traceparent` when present, propagated to the Account Service, logged by both services, and echoed in responses.

Domain metrics separate financial outcomes from transport metrics. Examples include accepted events, duplicate replays, idempotency conflicts, Account Service unavailability, circuit-open protection, applied transactions, and currency conflicts.

## Design Notes

Additional architecture and technical decision documentation:

- [Architecture](docs/architecture.md)
- [Requirements Traceability](docs/requirements-traceability.md)
- [Delivery Readiness Review](docs/delivery-readiness-review.md)
- [Technical Decisions](docs/technical-decisions.md)
- [C4 Context Diagram](docs/diagrams/c4-context.mmd)
- [C4 Container Diagram](docs/diagrams/c4-container.mmd)
- [Gateway Component Diagram](docs/diagrams/c4-component-gateway.mmd)
- [Account Service Component Diagram](docs/diagrams/c4-component-account-service.mmd)
