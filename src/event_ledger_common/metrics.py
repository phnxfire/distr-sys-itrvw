from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Any


class MetricsRegistry:
    def __init__(self) -> None:
        # This deliberately stays in memory for the take-home. The shape mirrors
        # what would normally be exported to Prometheus or another metrics sink.
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
        key = f"{method} {path}"
        with self._lock:
            # The lock protects counters when the ASGI server handles concurrent
            # requests in the same process.
            self._request_counts[key] += 1
            if status_code >= 500:
                self._error_counts[key] += 1

            latency = self._latency_ms[key]
            latency["count"] += 1
            latency["sum"] += duration_ms
            latency["max"] = max(latency["max"], duration_ms)

    def snapshot(self) -> dict[str, Any]:
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
