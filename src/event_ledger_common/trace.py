from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

TRACE_HEADER = "X-Trace-Id"

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    return uuid4().hex


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(trace_id: str):
    return _trace_id.set(trace_id)


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


def trace_id_from_header(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or new_trace_id()
