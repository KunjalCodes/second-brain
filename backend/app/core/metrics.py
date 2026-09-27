"""In-process operational metrics (Phase 11, M5; D6: JSON, no dependency).

Counters and fixed-bucket latency histograms kept in memory by the request
middleware and the error handlers, exposed -- together with gauges computed
from PostgreSQL on demand -- by `GET /internal/metrics` (operator token).

Labels are deliberately coarse: a route TEMPLATE (`/memories/{memory_id}`),
never a raw path or query; an error CODE; a provider name. No user ids,
emails, content or search text ever become a label. Counters reset when the
process restarts (acceptable for staging; stated in the response).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

#: Upper bounds (ms) of the latency histogram buckets; the last is +inf.
LATENCY_BUCKETS_MS: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)

#: Route groups reported as dedicated latency series (docs/PHASE11_PLAN.md 9.2).
_GROUPS = (
    ("graph", ("/graph",)),
    ("retrieval", ("/search", "/memories/search", "/entities/search")),
    ("agent", ("/agent-turn",)),
    ("auth", ("/auth",)),
)


class _Histogram:
    __slots__ = ("buckets", "count", "total_ms")

    def __init__(self) -> None:
        self.buckets = [0] * (len(LATENCY_BUCKETS_MS) + 1)
        self.count = 0
        self.total_ms = 0.0

    def observe(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        for i, bound in enumerate(LATENCY_BUCKETS_MS):
            if ms <= bound:
                self.buckets[i] += 1
                return
        self.buckets[-1] += 1

    def snapshot(self) -> dict[str, object]:
        labels = [f"le_{int(b)}" for b in LATENCY_BUCKETS_MS] + ["le_inf"]
        return {
            "count": self.count,
            "mean_ms": round(self.total_ms / self.count, 1) if self.count else None,
            "buckets": dict(zip(labels, self.buckets, strict=True)),
        }


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.started_at = datetime.now(UTC)
        self._start_monotonic = time.monotonic()
        self.requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self.latency: dict[str, _Histogram] = defaultdict(_Histogram)
        self.errors: dict[str, int] = defaultdict(int)
        self.rate_limited: dict[str, int] = defaultdict(int)
        self.auth_failures: dict[str, int] = defaultdict(int)

    def observe_request(self, *, method: str, route: str, status: int, ms: float) -> None:
        with self._lock:
            self.requests[(method, route, f"{status // 100}xx")] += 1
            self.latency["all"].observe(ms)
            for group, prefixes in _GROUPS:
                if any(route.startswith(p) or p in route for p in prefixes):
                    self.latency[group].observe(ms)

    def observe_error(self, *, code: str, status: int, details: dict | None = None) -> None:
        with self._lock:
            self.errors[code] += 1
            if status == 429:
                limit_class = (details or {}).get("limit_class", "unknown")
                self.rate_limited[str(limit_class)] += 1
            if status in (401, 403):
                self.auth_failures[code] += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            by_status: dict[str, int] = defaultdict(int)
            for (_, _, status_class), n in self.requests.items():
                by_status[status_class] += n
            return {
                "since": self.started_at.isoformat(),
                "uptime_seconds": round(time.monotonic() - self._start_monotonic),
                "requests_total": sum(self.requests.values()),
                "requests_by_status_class": dict(by_status),
                "requests_by_route": {
                    f"{m} {r} {s}": n for (m, r, s), n in sorted(self.requests.items())
                },
                "latency_ms": {k: h.snapshot() for k, h in sorted(self.latency.items())},
                "errors_by_code": dict(self.errors),
                "rate_limited_by_class": dict(self.rate_limited),
                "auth_failures_by_code": dict(self.auth_failures),
            }


metrics = Metrics()
