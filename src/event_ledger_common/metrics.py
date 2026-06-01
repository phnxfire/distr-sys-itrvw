"""Small in-process metrics registry used by /metrics endpoints.

Engineering view: this lightweight registry gives deterministic local metrics
without requiring Prometheus or OpenTelemetry infrastructure for the take-home.
Architecture view: the JSON shape mirrors counters and histograms that would be
exported to a production metrics backend.
Business view: domain counters make ledger outcomes visible, not just HTTP
traffic.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Any


class MetricsRegistry:
    """Thread-safe request and domain metric accumulator.

    The project keeps metrics in process to avoid adding infrastructure to the
    take-home exercise. The shape intentionally mirrors what would become
    Prometheus counters and histograms in a production deployment.
    """

    def __init__(self) -> None:
        """Initialize empty in-memory counters.

        Engineering view: the lock keeps updates safe when FastAPI executes
        request handlers concurrently in the same process.
        """

        self._lock = Lock()
        self._request_counts: dict[str, int] = defaultdict(int)
        self._error_counts: dict[str, int] = defaultdict(int)
        self._domain_counts: dict[str, int] = defaultdict(int)
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
        """Record one completed HTTP request.

        Operations view: request count, server error count, and duration are the
        basic RED signals needed to understand service health.
        """

        key = f"{method} {path}"
        with self._lock:
            self._request_counts[key] += 1
            if status_code >= 500:
                self._error_counts[key] += 1

            latency = self._latency_ms[key]
            latency["count"] += 1
            latency["sum"] += duration_ms
            latency["max"] = max(latency["max"], duration_ms)

    def record_domain_event(self, name: str, *, amount: int = 1) -> None:
        """Increment a named business counter.

        Request metrics explain transport behavior; domain counters explain
        ledger behavior such as accepted events, duplicate replays, conflicts,
        and downstream unavailability. Keeping both views separate makes the
        service easier to operate and easier to discuss in an architecture
        review.
        """

        if amount < 1:
            raise ValueError("domain metric amount must be positive")
        with self._lock:
            self._domain_counts[name] += amount

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable metrics snapshot.

        Architecture view: handlers expose a read-only copy so callers cannot
        mutate the live registry.
        """

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
                "domainEvents": dict(self._domain_counts),
            }
