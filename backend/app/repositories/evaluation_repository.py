"""Evaluation-run persistence (Phase 7, PHASE7_PLAN.md section 6/7).

**Deliberately not a `UserScopedRepository`.** An evaluation run has no
`user_id` at all -- it is operational/developer metadata about how well the
system performs, not any one user's data (see `app.models.evaluation`'s
module docstring). Exempted from the `select`/`update`/`delete` import ban
in `tests/unit/test_sql_safety.py`, for a different reason than
`extraction_queue.py`/`ingestion_queue.py`'s cross-user-by-design
exemption: there is no user dimension here to isolate in the first place.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvaluationRun as RunRow
from app.db.models import EvaluationTaskResult as TaskRow
from app.models.evaluation import (
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationTaskResult,
    EvaluationTaskStatus,
)


def _run_to_domain(row: RunRow) -> EvaluationRun:
    return EvaluationRun(
        id=row.id,
        harness=row.harness,
        dataset_version=row.dataset_version,
        mode=row.mode,
        llm_provider=row.llm_provider,
        llm_model=row.llm_model,
        status=EvaluationRunStatus(row.status),
        total_tasks=row.total_tasks,
        config=row.config,
        summary=row.summary,
        git_commit=row.git_commit,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _task_to_domain(row: TaskRow) -> EvaluationTaskResult:
    return EvaluationTaskResult(
        id=row.id,
        run_id=row.run_id,
        task_id=row.task_id,
        category=row.category,
        status=EvaluationTaskStatus(row.status),
        passed=row.passed,
        metrics=row.metrics,
        error_detail=row.error_detail,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


class EvaluationRunRepository:
    """Run-level CRUD. No `user_id` filtering -- see module docstring."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        run_id: str,
        harness: str,
        dataset_version: str,
        mode: str,
        llm_provider: str | None,
        llm_model: str | None,
        total_tasks: int,
        config: dict | None,
        git_commit: str | None,
        now: datetime,
    ) -> EvaluationRun:
        stmt = (
            insert(RunRow)
            .values(
                id=run_id,
                harness=harness,
                dataset_version=dataset_version,
                mode=mode,
                llm_provider=llm_provider,
                llm_model=llm_model,
                status=EvaluationRunStatus.PENDING.value,
                total_tasks=total_tasks,
                config=config,
                git_commit=git_commit,
                created_at=now,
            )
            .returning(RunRow)
        )
        result = await self._session.execute(stmt)
        return _run_to_domain(result.scalar_one())

    async def get(self, *, run_id: str) -> EvaluationRun | None:
        row = await self._session.get(RunRow, run_id)
        return _run_to_domain(row) if row is not None else None

    async def find_resumable(
        self,
        *,
        harness: str,
        dataset_version: str,
        mode: str,
        llm_provider: str | None,
        llm_model: str | None,
    ) -> EvaluationRun | None:
        """The most recent run for this exact configuration that is not yet
        finished -- what a later invocation resumes instead of starting a
        fresh run and losing already-completed task results. Matched on
        `llm_provider`/`llm_model` too: a `real_llm` run against a
        different model is a different run, never silently continued under
        the old one's identity.
        """
        provider_filter = (
            RunRow.llm_provider.is_(None)
            if llm_provider is None
            else RunRow.llm_provider == llm_provider
        )
        model_filter = (
            RunRow.llm_model.is_(None) if llm_model is None else RunRow.llm_model == llm_model
        )
        stmt = (
            select(RunRow)
            .where(
                RunRow.harness == harness,
                RunRow.dataset_version == dataset_version,
                RunRow.mode == mode,
                provider_filter,
                model_filter,
                RunRow.status.in_(
                    (EvaluationRunStatus.PENDING.value, EvaluationRunStatus.IN_PROGRESS.value)
                ),
            )
            .order_by(RunRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _run_to_domain(row) if row is not None else None

    async def find_previous_completed(
        self, *, harness: str, dataset_version: str, mode: str, before: datetime
    ) -> EvaluationRun | None:
        """The most recent `COMPLETED` run of this exact configuration
        before `before` -- what a regression comparison diffs against.
        """
        stmt = (
            select(RunRow)
            .where(
                RunRow.harness == harness,
                RunRow.dataset_version == dataset_version,
                RunRow.mode == mode,
                RunRow.status == EvaluationRunStatus.COMPLETED.value,
                RunRow.created_at < before,
            )
            .order_by(RunRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _run_to_domain(row) if row is not None else None

    async def list_recent(
        self,
        *,
        harness: str | None = None,
        limit: int = 20,
    ) -> list[EvaluationRun]:
        stmt = select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
        if harness is not None:
            stmt = stmt.where(RunRow.harness == harness)
        result = await self._session.execute(stmt)
        return [_run_to_domain(row) for row in result.scalars()]

    async def mark_started(self, *, run_id: str, now: datetime) -> None:
        stmt = (
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.started_at.is_(None))
            .values(status=EvaluationRunStatus.IN_PROGRESS.value, started_at=now)
        )
        await self._session.execute(stmt)

    async def mark_completed(self, *, run_id: str, summary: dict, now: datetime) -> None:
        stmt = (
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=EvaluationRunStatus.COMPLETED.value, summary=summary, completed_at=now)
        )
        await self._session.execute(stmt)

    async def update_summary(self, *, run_id: str, summary: dict) -> None:
        """Persists a summary snapshot without touching `status`/
        `completed_at` -- for a still-resumable (partial/rate-limited) run,
        so it can be inspected honestly (`eval/report.py`) before it
        finishes, distinct from `mark_completed`'s "this run is done" claim.
        """
        stmt = update(RunRow).where(RunRow.id == run_id).values(summary=summary)
        await self._session.execute(stmt)

    async def mark_failed(self, *, run_id: str, now: datetime) -> None:
        stmt = (
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(status=EvaluationRunStatus.FAILED.value, completed_at=now)
        )
        await self._session.execute(stmt)


class EvaluationTaskResultRepository:
    """Task-level CRUD -- the durable checkpoint a resumed run reads."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        result_id: str,
        run_id: str,
        task_id: str,
        category: str,
        status: EvaluationTaskStatus,
        passed: bool | None,
        metrics: dict | None,
        error_detail: str | None,
        now: datetime,
    ) -> EvaluationTaskResult:
        """Idempotent: a retried attempt at the same `(run_id, task_id)`
        overwrites the previous row rather than creating a duplicate
        (`evaluation_task_results_one_per_task` -- "duplicate task
        completion must be prevented," Phase 7's own explicit requirement).
        """
        stmt = (
            insert(TaskRow)
            .values(
                id=result_id,
                run_id=run_id,
                task_id=task_id,
                category=category,
                status=status.value,
                passed=passed,
                metrics=metrics,
                error_detail=error_detail,
                started_at=now,
                completed_at=now,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["run_id", "task_id"],
                set_={
                    "category": category,
                    "status": status.value,
                    "passed": passed,
                    "metrics": metrics,
                    "error_detail": error_detail,
                    "completed_at": now,
                },
            )
            .returning(TaskRow)
        )
        result = await self._session.execute(stmt)
        return _task_to_domain(result.scalar_one())

    async def list_for_run(self, *, run_id: str) -> list[EvaluationTaskResult]:
        stmt = select(TaskRow).where(TaskRow.run_id == run_id).order_by(TaskRow.created_at)
        result = await self._session.execute(stmt)
        return [_task_to_domain(row) for row in result.scalars()]

    async def completed_task_ids(self, *, run_id: str) -> set[str]:
        """Task ids already `COMPLETED` in this run -- what a resumed run
        skips, per Phase 7's "completed tasks must not run again during
        normal resume" requirement.
        """
        stmt = select(TaskRow.task_id).where(
            TaskRow.run_id == run_id, TaskRow.status == EvaluationTaskStatus.COMPLETED.value
        )
        result = await self._session.execute(stmt)
        return set(result.scalars())
