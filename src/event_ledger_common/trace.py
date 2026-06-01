"""Trace context helpers used by service middleware and outbound clients.

Engineering view: the helpers centralize header parsing and async context
management so request handlers do not duplicate tracing logic.
Architecture view: this module is the thin substitute for full OpenTelemetry in
the take-home scope while preserving W3C trace context compatibility.
Business view: trace IDs make a single financial event submission explainable
across Gateway logs, Account Service logs, and client responses.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from uuid import uuid4

TRACE_HEADER = "X-Trace-Id"
TRACEPARENT_HEADER = "traceparent"
_TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<trace_flags>[0-9a-f]{2})$"
)

# ContextVar gives each asyncio request task its own trace ID without relying on
# thread-local state, which is unsafe for async request handling.
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    """Create a new opaque trace identifier.

    Engineering view: UUID hex values are simple, collision-resistant local
    identifiers and also match W3C trace ID formatting.
    """

    return uuid4().hex


def get_trace_id() -> str:
    """Return the active request trace identifier.

    Architecture view: logging, metrics, and outbound clients can access trace
    context without threading it through every function signature.
    """

    return _trace_id.get()


def set_trace_id(trace_id: str):
    """Bind a trace identifier to the current async request context.

    Engineering view: this keeps concurrent FastAPI requests isolated even when
    they run on the same event loop.
    """

    return _trace_id.set(trace_id)


def reset_trace_id(token) -> None:
    """Restore the previous trace context after request processing.

    Engineering view: explicit cleanup prevents one request's trace ID from
    leaking into later work handled by the same process.
    """

    _trace_id.reset(token)


def trace_id_from_header(value: str | None) -> str:
    """Use a caller-supplied trace ID or create a new one when absent.

    Business view: callers that already have a correlation ID can keep their
    audit trail intact; otherwise the Gateway starts one.
    """

    normalized = (value or "").strip()
    return normalized or new_trace_id()


def trace_id_from_headers(trace_id: str | None, traceparent: str | None) -> str:
    """Resolve trace context from W3C traceparent, X-Trace-Id, or a new ID.

    Architecture view: W3C traceparent wins because it is the professional
    distributed tracing standard; X-Trace-Id remains useful for local demos.
    """

    traceparent_trace_id = trace_id_from_traceparent(traceparent)
    if traceparent_trace_id:
        return traceparent_trace_id
    return trace_id_from_header(trace_id)


def trace_id_from_traceparent(value: str | None) -> str | None:
    """Extract the W3C trace ID from a traceparent header.

    Engineering view: invalid or all-zero trace IDs are ignored instead of
    poisoning logs with non-compliant correlation data.
    """

    normalized = (value or "").strip().lower()
    match = _TRACEPARENT_PATTERN.match(normalized)
    if not match:
        return None
    trace_id = match.group("trace_id")
    if trace_id == "0" * 32:
        return None
    return trace_id


def traceparent_from_trace_id(trace_id: str) -> str | None:
    """Create a W3C traceparent header for valid 32-character trace IDs.

    Architecture view: outbound Account Service calls can be upgraded to full
    OpenTelemetry later without changing the public API contract.
    """

    normalized = trace_id.lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized) or normalized == "0" * 32:
        return None
    span_id = uuid4().hex[:16]
    return f"00-{normalized}-{span_id}-01"
