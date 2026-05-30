# Architecture

## Goals

Event Ledger is designed to demonstrate a production-oriented slice of a distributed financial event processor. The implementation favors simple, explicit mechanisms over infrastructure-heavy choices so the behavior can be reviewed and tested quickly.

Primary design goals:

- accept valid financial transaction events through a public Gateway
- prevent duplicate `eventId` submissions from double-applying money
- preserve chronological event history even when events arrive out of order
- keep Gateway and Account Service state physically separate
- expose enough logs, metrics, and health signals to operate the system
- degrade clearly when the Account Service is unavailable

## C4 Diagrams

The C4 diagram source files are stored under [docs/diagrams](diagrams).

### C4 Level 1: System Context

```mermaid
C4Context
  title Event Ledger - System Context
  Person(client, "Browser / API Client", "Submits and reads financial transaction events")
  System(eventLedger, "Event Ledger", "Processes transaction events and exposes account state")
  System_Ext(upstream, "Upstream Systems", "Mainframe batch, payment rails, and other event producers")

  Rel(client, eventLedger, "Submits events and queries status", "HTTPS/JSON")
  Rel(upstream, eventLedger, "Delivers transaction events", "HTTPS/JSON")
```

### C4 Level 2: Containers

```mermaid
C4Container
  title Event Ledger - Containers
  Person(client, "Browser / API Client", "External caller")
  Container(gateway, "Event Gateway API", "FastAPI", "Validates events, enforces idempotency, stores accepted event records, calls Account Service")
  Container(account, "Account Service", "FastAPI", "Applies account transactions, protects account idempotency, computes balances")
  ContainerDb(gatewayDb, "Gateway SQLite DB", "SQLite", "Accepted event records")
  ContainerDb(accountDb, "Account SQLite DB", "SQLite", "Applied account transactions")

  Rel(client, gateway, "POST /events, GET /events, GET /accounts/{id}/balance", "HTTPS/JSON")
  Rel(gateway, account, "POST /accounts/{id}/transactions and account reads", "HTTP/JSON with X-Trace-Id")
  Rel(gateway, gatewayDb, "Reads/writes", "SQLite")
  Rel(account, accountDb, "Reads/writes", "SQLite")
```

## Request Flow

```mermaid
sequenceDiagram
  autonumber
  participant Client
  participant Gateway as Event Gateway API
  participant GatewayDB as Gateway SQLite
  participant Account as Account Service
  participant AccountDB as Account SQLite

  Client->>Gateway: POST /events
  Gateway->>Gateway: Validate payload and trace ID
  Gateway->>GatewayDB: Look up eventId
  alt duplicate eventId with same details
    Gateway-->>Client: 200 OK original event
  else duplicate eventId with different details
    Gateway-->>Client: 409 Conflict
  else new event
    Gateway->>Account: POST /accounts/{accountId}/transactions with X-Trace-Id
    Account->>AccountDB: Insert transaction if eventId not present
    Account-->>Gateway: 201 Created or 200 duplicate replay
    Gateway->>GatewayDB: Store applied event record
    Gateway-->>Client: 201 Created
  end
```

## Data Ownership

The Gateway owns event records used by public event reads:

- `event_id`
- `account_id`
- transaction details
- original `event_timestamp`
- metadata
- status and audit timestamps

The Account Service owns applied account transactions and balance calculation:

- `event_id`
- `account_id`
- transaction details
- original `event_timestamp`
- applied timestamp

This avoids shared mutable state. The same `eventId` is enforced as unique in both databases because idempotency must hold at both the public boundary and the internal money-moving boundary.

## Idempotency Strategy

The Gateway checks its local event table before calling the Account Service. If the `eventId` exists with identical event details, the Gateway returns the original event with `200 OK` and does not call the Account Service.

The Account Service also has a unique `event_id` constraint. This makes Gateway retries safe: if a timeout happens after the Account Service applied a transaction, the retry replays the same event and the Account Service returns its existing transaction without changing the balance again.

If the same `eventId` is submitted with different details, both services treat it as an idempotency-key conflict and return `409 Conflict`.

## Out-of-Order Events

Arrival order is not used for business ordering. Both services persist the original `eventTimestamp` and sort history by `eventTimestamp`, then `eventId` as a deterministic tie-breaker.

Balances are computed from the complete set of applied transactions:

```text
balance = sum(CREDIT amounts) - sum(DEBIT amounts)
```

This makes balance independent of event arrival order.

## Resiliency

The Gateway uses timeout plus retry with exponential backoff for calls to the Account Service.

This pattern is intentionally bounded:

- no infinite retries
- no long client hangs
- no hidden background queue
- clear `503 Service Unavailable` when the Account Service cannot be reached

The design is safe because both the Gateway and Account Service are idempotent by `eventId`.

## Graceful Degradation

When the Account Service is unreachable:

- `POST /events` returns `503 Service Unavailable`
- existing `GET /events/{id}` still works from the Gateway database
- existing `GET /events?account=...` still works from the Gateway database
- Gateway balance/account proxy endpoints return `503 Service Unavailable` with clear details

## Observability

Both services include:

- JSON structured logs
- `X-Trace-Id` propagation
- request metrics
- health checks with database connectivity diagnostics

The trace ID is a lightweight substitute for full OpenTelemetry in this scoped exercise. Full OTel export could be added later without changing the public API.
