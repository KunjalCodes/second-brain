"""Persistent, resumable evaluation-run tracking (Phase 7, PHASE7_PLAN.md
section 6/7).

This is the checkpoint/resume layer every harness's real-LLM section
integrates with: `EvaluationRunTracker.start_or_resume` finds (or creates)
the durable `evaluation_runs` row for one `(harness, dataset_version, mode,
llm_provider, llm_model)` configuration; `completed_task_ids` tells a caller
which tasks to skip; `record_task` persists one task's outcome idempotently
(`ON CONFLICT (run_id, task_id) DO UPDATE` -- a re-attempt overwrites, never
duplicates); `finalize` marks the run `COMPLETED` once every task has
reached a terminal status, and always returns a computed summary so a
still-partial (e.g. rate-limited) run can still be reported honestly.

**Never touches Neo4j or canonical memory.** This module only writes to the
two Phase 7 PostgreSQL tables -- the harnesses it supports still seed and
query real (synthetic, cleaned-up) users through the unchanged
`MemoryService`/`HybridRetrievalService`/`AgentService`, exactly as before.

**Never writes raw memory/document content.** `TaskOutcome.metrics` must
only ever hold ids/counts/scores/strings the caller has already decided are
safe to persist as evaluation metadata (task ids, tool names, latency,
token counts) -- see `app.models.evaluation`'s module docstring.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.evaluation import EvaluationTaskStatus
from app.repositories.evaluation_repository import (
    EvaluationRunRepository,
    EvaluationTaskResultRepository,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def current_git_commit() -> str | None:
    """Best-effort -- `None` (never raises) outside a git checkout or if
    `git` is unavailable, since this is provenance metadata, not something
    a run should fail over.
    """
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed args, no user input
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@dataclass(frozen=True)
class TaskOutcome:
    """What a harness reports back to the tracker for one task.

    `status=COMPLETED` with `passed=False` is a model-judgment failure
    (the task ran cleanly but got the wrong answer); `status=ERRORED`/
    `RATE_LIMITED` is an infrastructure failure -- these are never
    conflated (see `app.models.evaluation.EvaluationTaskStatus`'s own
    docstring). `metrics` holds only the keys actually available for this
    task; omit a key entirely rather than writing `0`/`None` for something
    that was never computed.
    """

    status: EvaluationTaskStatus
    passed: bool | None = None
    metrics: dict | None = None
    error_detail: str | None = None


class EvaluationRunTracker:
    """Owns the checkpoint/resume lifecycle for one evaluation invocation.

    Constructed fresh per harness invocation (cheap -- it holds no
    per-call state beyond `now_fn`/`id_fn`, mirroring `ExtractionRunner`'s
    own construction pattern).
    """

    def __init__(
        self,
        db,  # noqa: ANN001 - app.db.engine.Database, kept untyped to avoid an eval->app import cycle risk
        *,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._db = db
        self._now = now_fn
        self._new_id = id_fn

    async def start_or_resume(
        self,
        *,
        harness: str,
        dataset_version: str,
        mode: str,
        task_ids: list[str],
        llm_provider: str | None = None,
        llm_model: str | None = None,
        config: dict | None = None,
    ) -> str:
        """Returns a `run_id` -- either a freshly created run, or the id of
        the most recent not-yet-finished run for this exact configuration
        (Phase 7's "CHECK EXISTING CHECKPOINT" step). A resumed run's
        `total_tasks` is NOT updated even if `task_ids` differs in length
        from the original invocation -- a dataset change should produce a
        new run (a different `dataset_version` string), not silently
        redefine what an in-flight run means.
        """
        now = self._now()
        async with self._db.session() as session:
            runs = EvaluationRunRepository(session)
            existing = await runs.find_resumable(
                harness=harness,
                dataset_version=dataset_version,
                mode=mode,
                llm_provider=llm_provider,
                llm_model=llm_model,
            )
            if existing is not None:
                return existing.id
            run = await runs.create(
                run_id=self._new_id(),
                harness=harness,
                dataset_version=dataset_version,
                mode=mode,
                llm_provider=llm_provider,
                llm_model=llm_model,
                total_tasks=len(task_ids),
                config=config,
                git_commit=current_git_commit(),
                now=now,
            )
            await runs.mark_started(run_id=run.id, now=now)
        return run.id

    async def completed_task_ids(self, *, run_id: str) -> set[str]:
        """Task ids already `COMPLETED` in this run -- a caller skips
        these on resume ("completed tasks must not run again during
        normal resume").
        """
        async with self._db.session() as session:
            return await EvaluationTaskResultRepository(session).completed_task_ids(run_id=run_id)

    async def record_task(
        self, *, run_id: str, task_id: str, category: str, outcome: TaskOutcome
    ) -> None:
        """Idempotent: calling this twice for the same `(run_id, task_id)`
        overwrites, never duplicates (the `evaluation_task_results_one_per_task`
        constraint plus `ON CONFLICT ... DO UPDATE` in the repository).
        """
        now = self._now()
        async with self._db.session() as session:
            await EvaluationTaskResultRepository(session).upsert(
                result_id=self._new_id(),
                run_id=run_id,
                task_id=task_id,
                category=category,
                status=outcome.status,
                passed=outcome.passed,
                metrics=outcome.metrics,
                error_detail=outcome.error_detail,
                now=now,
            )

    async def finalize(self, *, run_id: str, task_ids: list[str]) -> dict:
        """Computes a summary from every persisted task result and marks
        the run `COMPLETED` only if every id in `task_ids` has reached a
        terminal status (`COMPLETED`/`SKIPPED`). A run left with any
        `PENDING`/`RATE_LIMITED`/`ERRORED` task stays resumable (its
        PostgreSQL `status` is left as `IN_PROGRESS`) -- the next
        invocation's `start_or_resume` will find and continue it. Always
        returns the summary, even for a partial run, so a rate-limited
        attempt can still be reported honestly (Phase 7's own "persist
        completed results... allow the run to resume later" requirement).
        """
        now = self._now()
        async with self._db.session() as session:
            results = await EvaluationTaskResultRepository(session).list_for_run(run_id=run_id)
        by_task_id = {r.task_id: r for r in results}
        summary = _build_summary(results, task_ids=task_ids)

        terminal = {EvaluationTaskStatus.COMPLETED, EvaluationTaskStatus.SKIPPED}
        finished = all(
            task_id in by_task_id and by_task_id[task_id].status in terminal for task_id in task_ids
        )
        async with self._db.session() as session:
            runs = EvaluationRunRepository(session)
            if finished:
                await runs.mark_completed(run_id=run_id, summary=summary, now=now)
            else:
                # Still resumable -- persist the summary snapshot anyway
                # (useful for inspecting a partial/rate-limited run) without
                # closing the run.
                await runs.update_summary(run_id=run_id, summary=summary)
        return summary


def _build_summary(results: list, *, task_ids: list[str]) -> dict:  # noqa: ANN001 - list[EvaluationTaskResult]
    """Aggregates persisted task results into a summary dict. Every count
    is computed directly from `results` -- no fabricated values.
    `mean_recall`/`mean_precision` are computed only over tasks whose
    `metrics` actually contain that key (explicit N/A semantics: a task
    with no `recall` key is excluded from the mean, never treated as 0).
    """
    by_task_id = {r.task_id: r for r in results}
    completed = [r for r in results if r.status == EvaluationTaskStatus.COMPLETED]
    rate_limited = [r for r in results if r.status == EvaluationTaskStatus.RATE_LIMITED]
    errored = [r for r in results if r.status == EvaluationTaskStatus.ERRORED]
    skipped = [r for r in results if r.status == EvaluationTaskStatus.SKIPPED]
    incomplete = [task_id for task_id in task_ids if task_id not in by_task_id]

    passed_among_completed = [r for r in completed if r.passed]

    def _values(key: str) -> list[float]:
        return [r.metrics[key] for r in completed if r.metrics is not None and key in r.metrics]

    def _mean_metric(key: str) -> float | None:
        values = _values(key)
        return sum(values) / len(values) if values else None

    def _sum_metric(key: str) -> float | None:
        """`None` if no completed task has this key at all -- a task that
        DOES have the key but happens to report `0` still counts (unlike a
        missing key, which is excluded entirely, never treated as 0).
        """
        values = _values(key)
        return sum(values) if values else None

    return {
        "total_tasks": len(task_ids),
        "completed": len(completed),
        "passed": len(passed_among_completed),
        "rate_limited": len(rate_limited),
        "errored": len(errored),
        "skipped": len(skipped),
        "incomplete": len(incomplete),
        "incomplete_task_ids": incomplete,
        "mean_recall": _mean_metric("recall"),
        "mean_precision": _mean_metric("precision"),
        "mean_latency_ms": _mean_metric("latency_ms"),
        "total_input_tokens": _sum_metric("input_tokens"),
        "total_output_tokens": _sum_metric("output_tokens"),
    }
