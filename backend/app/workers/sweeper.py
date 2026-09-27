"""In-process periodic recovery (Phase 11, M2; docs/PHASE11_PLAN.md section 6).

One asyncio task, started by the app lifespan, that every
`SWEEP_INTERVAL_SECONDS` runs, in order:

1. the extraction sweep (`run_sweep_tick`): reclaim expired leases, make retry
   exhaustion explicit, fail the targets of abandoned jobs, process a bounded
   batch of claimable jobs;
2. the ingestion sweep (`run_ingestion_sweep_tick`): the same for documents --
   previously never invoked by anything (J2);
3. session cleanup: delete a bounded batch of sessions that can never
   authenticate again (S2).

Design:

* **No new infrastructure.** The PostgreSQL queue tables ARE the queue; this
  task only drives them. No Redis, no Celery, no separate worker process.
* **One sweeper at a time.** Each pass holds a PostgreSQL transaction-level
  advisory lock (`pg_try_advisory_xact_lock`). With several instances only
  one sweeps; the others skip that pass. Correctness never depends on the lock
  (claims use `SKIP LOCKED` and completions are lease-owner checked); it only
  avoids redundant work.
* **Bounded.** Every step is capped by existing batch-size settings.
* **Never dies.** Each step's failure is logged and counted. Consecutive
  failed passes back the interval off exponentially (capped at 10 minutes), so
  an outage does not produce a log storm. A successful pass resets it.
* **Startup.** The first pass runs `SWEEP_INITIAL_DELAY_SECONDS` after boot,
  so work queued before a restart is picked up promptly. Jobs a dead process
  held `in_progress` are recovered once their lease expires (the lease is
  renewed while a job is actively processed, so a live job is never stolen).
* **Shutdown.** `stop()` signals the loop and waits up to
  `SWEEP_SHUTDOWN_GRACE_SECONDS` for the current pass to finish, then cancels
  it. A cancelled job stays `in_progress` and is recovered by lease expiry;
  completion writes are conditional on lease ownership, so an interrupted job
  can never write a stale result.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import Settings
from app.db.engine import Database
from app.documents.ingestion import run_ingestion_sweep_tick
from app.embeddings.base import EmbeddingClient
from app.extraction.runner import run_sweep_tick
from app.graph.connection import GraphClient
from app.llm.base import LLMClient
from app.repositories.auth_repository import delete_dead_sessions
from app.storage.base import DocumentStorage

logger = logging.getLogger(__name__)

#: Arbitrary constant identifying "the sweeper" among advisory locks.
SWEEP_ADVISORY_LOCK_KEY = 0x5EEB_11
_MAX_BACKOFF_SECONDS = 600
_SESSION_RETENTION = timedelta(days=7)
_SESSION_CLEANUP_BATCH = 1000


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SweepStats:
    """Process-local observability for `/internal/metrics` (Phase 11, M5)."""

    passes: int = 0
    skipped_locked: int = 0
    failed_steps: int = 0
    consecutive_failures: int = 0
    last_pass_at: datetime | None = None
    last_pass_ms: float | None = None
    last_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    last_error: str | None = None


class Sweeper:
    def __init__(
        self,
        *,
        db: Database,
        graph_client: GraphClient,
        llm_client_fn: Callable[[], LLMClient],
        embedding_client_fn: Callable[[], EmbeddingClient],
        storage_fn: Callable[[], DocumentStorage],
        settings: Settings,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = db
        self._graph = graph_client
        self._llm = llm_client_fn
        self._embeddings = embedding_client_fn
        self._storage = storage_fn
        self._settings = settings
        self._now = now_fn
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.stats = SweepStats()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="sweeper")
        logger.info(
            "sweeper_started interval_s=%s initial_delay_s=%s",
            self._settings.sweep_interval_seconds,
            self._settings.sweep_initial_delay_seconds,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(self._task), timeout=self._settings.sweep_shutdown_grace_seconds
            )
        except TimeoutError:
            logger.warning("sweeper_stop_timeout cancelling the in-flight pass")
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("sweeper_stopped")

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, waking early on stop. Returns True if stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _loop(self) -> None:
        if await self._sleep(self._settings.sweep_initial_delay_seconds):
            return
        while not self._stop.is_set():
            await self.run_once()
            interval = self._settings.sweep_interval_seconds
            if self.stats.consecutive_failures:
                interval = min(
                    interval * (2**self.stats.consecutive_failures), _MAX_BACKOFF_SECONDS
                )
            if await self._sleep(interval):
                return

    async def run_once(self) -> dict[str, dict[str, int]] | None:
        """One guarded pass. Returns per-step counts, or None if another
        instance holds the sweep lock. Never raises (except cancellation)."""
        started = time.perf_counter()
        counts: dict[str, dict[str, int]] = {}
        failed = False
        try:
            async with self._db.session() as lock_session:
                got = (
                    await lock_session.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"),
                        {"k": SWEEP_ADVISORY_LOCK_KEY},
                    )
                ).scalar()
                if not got:
                    self.stats.skipped_locked += 1
                    return None
                for name, step in (
                    ("extraction", self._extraction),
                    ("ingestion", self._ingestion),
                    ("sessions", self._sessions),
                ):
                    if self._stop.is_set():
                        break
                    try:
                        counts[name] = await step()
                    except Exception as exc:  # noqa: BLE001 - one step must not stop the others
                        failed = True
                        self.stats.failed_steps += 1
                        self.stats.last_error = f"{name}: {type(exc).__name__}"
                        logger.exception("sweeper_step_failed step=%s", name)
        except Exception as exc:  # noqa: BLE001 - e.g. PostgreSQL unreachable for the lock
            failed = True
            self.stats.failed_steps += 1
            self.stats.last_error = f"lock: {type(exc).__name__}"
            logger.warning("sweeper_pass_failed reason=%s", type(exc).__name__)

        self.stats.passes += 1
        self.stats.consecutive_failures = self.stats.consecutive_failures + 1 if failed else 0
        self.stats.last_pass_at = self._now()
        self.stats.last_pass_ms = round((time.perf_counter() - started) * 1000, 1)
        self.stats.last_counts = counts
        if any(v for step in counts.values() for v in step.values()):
            logger.info("sweeper_pass counts=%s ms=%s", counts, self.stats.last_pass_ms)
        return counts

    async def _extraction(self) -> dict[str, int]:
        return await run_sweep_tick(
            db=self._db,
            graph_client=self._graph,
            llm_client=self._llm(),
            embedding_client=self._embeddings(),
            settings=self._settings,
            worker_id="sweeper",
            now_fn=self._now,
        )

    async def _ingestion(self) -> dict[str, int]:
        return await run_ingestion_sweep_tick(
            db=self._db,
            storage=self._storage(),
            settings=self._settings,
            worker_id="sweeper-ingestion",
            now_fn=self._now,
        )

    async def _sessions(self) -> dict[str, int]:
        now = self._now()
        async with self._db.session() as session:
            deleted = await delete_dead_sessions(
                session,
                dead_before=now - _SESSION_RETENTION,
                idle_dead_before=now
                - _SESSION_RETENTION
                - timedelta(hours=self._settings.auth_session_idle_hours),
                limit=_SESSION_CLEANUP_BATCH,
            )
        return {"deleted": deleted}


_sweeper: Sweeper | None = None


def get_sweeper() -> Sweeper | None:
    """The process's running sweeper, if any (for metrics)."""
    return _sweeper


def set_sweeper(sweeper: Sweeper | None) -> None:
    global _sweeper
    _sweeper = sweeper
