"""Operator metrics (Phase 11, M5; docs/PHASE11_PLAN.md section 9.2).

`GET /internal/metrics` -- JSON, no metrics dependency (D6). Requires the
operator token (`X-Internal-Token`), like every other internal route: queue
depths, error rates and provider health are operational internals and are not
public. The body contains only aggregate numbers and coarse labels (route
templates, error codes, provider names) -- no user ids or content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.api.deps import InternalTokenDep
from app.core.metrics import metrics
from app.db.engine import get_database
from app.llm.resilience import all_provider_health
from app.repositories.ops_repository import queue_gauges
from app.workers.sweeper import get_sweeper

internal_router = APIRouter(prefix="/internal", tags=["internal"])


@internal_router.get(
    "/metrics", summary="Operational metrics (internal; requires X-Internal-Token)"
)
async def get_metrics(_: InternalTokenDep) -> dict[str, Any]:
    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "note": "In-process counters reset when the process restarts.",
        "http": metrics.snapshot(),
    }
    try:
        async with get_database().session() as session:
            body["queues"] = await queue_gauges(session, now=now)
    except Exception as exc:  # noqa: BLE001 - metrics must answer even in an outage
        body["queues"] = {"status": "unavailable", "reason": type(exc).__name__}

    body["llm_providers"] = {
        name: {
            "calls": h.calls,
            "successes": h.successes,
            "errors_by_kind": dict(h.errors),
            "cooldown_remaining_seconds": round(h.cooling_down(_monotonic()), 1),
            "cooldown_trips": h.cooldown_trips,
        }
        for name, h in all_provider_health().items()
    }
    sweeper = get_sweeper()
    if sweeper is None:
        body["sweeper"] = {"running": False}
    else:
        st = sweeper.stats
        body["sweeper"] = {
            "running": sweeper.running,
            "passes": st.passes,
            "skipped_locked": st.skipped_locked,
            "failed_steps": st.failed_steps,
            "consecutive_failures": st.consecutive_failures,
            "last_pass_at": st.last_pass_at.isoformat() if st.last_pass_at else None,
            "last_pass_ms": st.last_pass_ms,
            "last_counts": st.last_counts,
            "last_error": st.last_error,
        }
    return body


def _monotonic() -> float:
    import time

    return time.monotonic()
