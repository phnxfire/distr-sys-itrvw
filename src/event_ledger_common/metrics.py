"""Small in-process metrics registry used by /metrics endpoints."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Any


class MetricsRegistry:
    """Thread-safe request counter and latency accumulator."""

    def __init__(self) -> None:
        """Initialize empty in-memory counters."""

        self._lock = Lock()
        self._request_counts: dict[str, int] = defaultdict(int)
        self._error_counts: dict[str, int] = defaultdict(int)
        self._latency_ms: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "sum": 0.0, "max": 0.0}
        )

    def record_request(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        """Record one completed HTTP request."""

        key = f"{method} {path}"
        with self._lock:
            self._request_counts[key] += 1
            if status_code >= 500:
                self._error_counts[key] += 1

            latency = self._latency_ms[key]
            latency["count"] += 1
            latency["sum"] += duration_ms
            latency["max"] = max(latency["max"], duration_ms)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable metrics snapshot."""

        with self._lock:
            latency = {
                key: {
                    "count": int(value["count"]),
                    "avgMs": round(value["sum"] / value["count"], 3)
                    if value["count"]
                    else 0.0,
                    "maxMs": round(value["max"], 3),
                }
                for key, value in self._latency_ms.items()
            }
            return {
                "requests": dict(self._request_counts),
                "errors": dict(self._error_counts),
                "latency": latency,
            }
