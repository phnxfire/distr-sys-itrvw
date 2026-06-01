# Event Ledger

Event Ledger is a two-service take-home project for processing financial transaction events from upstream systems that can deliver duplicates and out-of-order events.

The solution focuses on correctness, service separation, observability, and graceful failure:

- idempotent `eventId` handling in both services
- chronological event history regardless of arrival order
- balance computation from applied account transactions
- independent SQLite databases per service
- structured JSON logs with propagated trace IDs
- health and metrics endpoints
- timeout plus retry with exponential backoff for Gateway to Account Service calls

## Architecture

The system has two independently runnable FastAPI services.

| Service | Responsibility | Local State |
| --- | --- | --- |
| Event Gateway API | Public API, validation, event idempotency, event history, trace generation, Account Service calls | SQLite via `GATEWAY_DB_PATH`, default `/tmp/event-ledger/gateway.sqlite` |
| Account Service | Internal account transaction application, account balance, account details | SQLite via `ACCOUNT_DB_PATH`, default `/tmp/event-ledger/account-service.sqlite` |

The Gateway and Account Service do not share database connections, tables, or in-process state. They communicate synchronously over REST.

See [docs/architecture.md](docs/architecture.md) for C4 diagrams and request-flow details.

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
| `GET` | `/metrics` | Simple JSON request/error/latency metrics |

### Account Service

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/accounts/{accountId}/transactions` | Apply a transaction to an account |
| `GET` | `/accounts/{accountId}/balance` | Get current account balance |
| `GET` | `/accounts/{accountId}` | Get balance and recent transactions |
| `GET` | `/health` | Account Service health check |
| `GET` | `/metrics` | Simple JSON request/error/latency metrics |

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

Then call the Gateway:

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

## Tests

Run the automated test suite:

```bash
make test
```

The tests cover validation, idempotency, out-of-order event listing, balance correctness, Account Service failure handling, retry behavior, trace propagation, and an end-to-end Gateway to Account Service flow.

## Diagrams

The Mermaid C4 source files live under `docs/diagrams/*.mmd`. Rendered SVG images can be regenerated with:

```bash
npm install
npm run render:diagrams
```

## Resiliency Choice

The Gateway uses **timeout plus retry with exponential backoff** for Account Service calls.

This is the best fit for a small synchronous REST system because many Account Service failures are transient: startup race, brief network failure, or short overload. The implementation bounds the blast radius with:

- a short per-request timeout
- a fixed maximum attempt count
- exponential backoff between attempts
- `503 Service Unavailable` responses when the Account Service remains unreachable

Retries are safe because the Account Service also enforces idempotency on `eventId`.

## Observability

Both services expose:

- `GET /health` for status and database diagnostics
- `GET /metrics` for request counts, error counts, and latency summaries
- JSON structured logs with `timestamp`, `level`, `service`, `trace_id`, and HTTP details

Trace IDs are generated at the Gateway when absent, accepted from `X-Trace-Id` when present, propagated to the Account Service via the same header, logged by both services, and echoed in responses.

## Design Notes

Additional architecture and technical decision documentation:

- [Architecture](docs/architecture.md)
- [Technical Decisions](docs/technical-decisions.md)
- [C4 Context Diagram](docs/diagrams/c4-context.mmd)
- [C4 Container Diagram](docs/diagrams/c4-container.mmd)
- [Gateway Component Diagram](docs/diagrams/c4-component-gateway.mmd)
- [Account Service Component Diagram](docs/diagrams/c4-component-account-service.mmd)
