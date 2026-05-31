from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from event_ledger_common.trace import get_trace_id

_CONFIGURED_LOGGERS: set[str] = set()


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        # JSON logs are intentionally flat so they are easy to grep locally and
        # easy for log aggregation systems to index in a real deployment.
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service_name,
            "trace_id": getattr(record, "trace_id", get_trace_id()),
            "message": record.getMessage(),
        }

        for key in (
            "http_method",
            "path",
            "status_code",
            "duration_ms",
            "event_id",
            "account_id",
            "attempt",
            "error",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Inject the current request trace ID into every log record emitted by
        # service code, even when the call site does not pass extra fields.
        record.trace_id = get_trace_id()
        return True


def get_logger(service_name: str) -> logging.Logger:
    logger = logging.getLogger(service_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if service_name not in _CONFIGURED_LOGGERS:
        # Logger setup is idempotent because tests create multiple app instances.
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter(service_name))
        handler.addFilter(TraceContextFilter())
        logger.handlers.clear()
        logger.addHandler(handler)
        _CONFIGURED_LOGGERS.add(service_name)

    return logger
