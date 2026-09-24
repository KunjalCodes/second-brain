"""Evaluation-run persistence and resume (Phase 7, PHASE7_PLAN.md section
6/7), against real PostgreSQL -- `eval/persistence.py`'s
`EvaluationRunTracker` is the checkpoint/resume layer every harness's
real-LLM section integrates with.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models.evaluation import EvaluationRunStatus, EvaluationTaskStatus  # noqa: E402
from app.repositories.evaluation_repository import EvaluationRunRepository  # noqa: E402
from eval.persistence import EvaluationRunTracker, TaskOutcome  # noqa: E402

pytestmark = pytest.mark.integration

JAN = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def harness() -> str:
    """A unique harness name per test, mirroring `user_id`'s role in every
    other integration test -- so tests never collide or depend on order.
    """
    return f"itest-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
async def _cleanup(db, harness):  # noqa: ANN001, ANN201
    yield
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM evaluation_runs WHERE harness = :h"), {"h": harness}
        )


class TestRunCreation:
    async def test_creates_a_pending_then_started_run(self, db, harness) -> None:  # noqa: ANN001
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="deterministic", task_ids=["t1", "t2"]
        )
        async with db.session() as session:
            run = await EvaluationRunRepository(session).get(run_id=run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.IN_PROGRESS
        assert run.total_tasks == 2
        assert run.started_at == JAN

    async def test_a_second_call_with_the_same_key_resumes_the_same_run(
        self,
        db,
        harness,  # noqa: ANN001
    ) -> None:
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        first = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="deterministic", task_ids=["t1"]
        )
        second = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="deterministic", task_ids=["t1"]
        )
        assert first == second

    async def test_a_different_llm_model_is_a_different_run(self, db, harness) -> None:  # noqa: ANN001
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        run_a = await tracker.start_or_resume(
            harness=harness,
            dataset_version="v1",
            mode="real_llm",
            task_ids=["t1"],
            llm_provider="gemini",
            llm_model="gemini-2.5-flash",
        )
        run_b = await tracker.start_or_resume(
            harness=harness,
            dataset_version="v1",
            mode="real_llm",
            task_ids=["t1"],
            llm_provider="gemini",
            llm_model="gemini-3.0",
        )
        assert run_a != run_b


class TestTaskPersistenceAndResume:
    async def test_completed_task_is_skipped_on_resume(self, db, harness) -> None:  # noqa: ANN001
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=["t1", "t2"]
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True),
        )
        done = await tracker.completed_task_ids(run_id=run_id)
        assert done == {"t1"}

    async def test_incomplete_task_resumes_and_run_stays_in_progress(
        self,
        db,
        harness,  # noqa: ANN001
    ) -> None:
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        task_ids = ["t1", "t2"]
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=task_ids
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True),
        )
        summary = await tracker.finalize(run_id=run_id, task_ids=task_ids)
        assert summary["incomplete"] == 1
        assert summary["incomplete_task_ids"] == ["t2"]

        async with db.session() as session:
            run = await EvaluationRunRepository(session).get(run_id=run_id)
        assert run.status is EvaluationRunStatus.IN_PROGRESS

        resumed_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=task_ids
        )
        assert resumed_id == run_id

    async def test_rate_limited_task_is_preserved_not_lost(self, db, harness) -> None:  # noqa: ANN001
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        task_ids = ["t1"]
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=task_ids
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.RATE_LIMITED, error_detail="HTTP 429"),
        )
        summary = await tracker.finalize(run_id=run_id, task_ids=task_ids)
        assert summary["rate_limited"] == 1
        assert summary["completed"] == 0

        async with db.session() as session:
            run = await EvaluationRunRepository(session).get(run_id=run_id)
        # A rate-limited task is not a finished outcome -- the run must
        # stay resumable, never silently marked COMPLETED.
        assert run.status is not EvaluationRunStatus.COMPLETED

    async def test_interrupted_run_resumes_and_reaches_completed(
        self,
        db,
        harness,  # noqa: ANN001
    ) -> None:
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        task_ids = ["t1", "t2", "t3"]
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=task_ids
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True),
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t2",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.RATE_LIMITED, error_detail="HTTP 429"),
        )
        await tracker.finalize(run_id=run_id, task_ids=task_ids)

        # A later invocation resumes: only t2/t3 need attempting.
        resumed_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=task_ids
        )
        already_done = await tracker.completed_task_ids(run_id=resumed_id)
        assert already_done == {"t1"}

        await tracker.record_task(
            run_id=resumed_id,
            task_id="t2",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True),
        )
        await tracker.record_task(
            run_id=resumed_id,
            task_id="t3",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=False),
        )
        summary = await tracker.finalize(run_id=resumed_id, task_ids=task_ids)
        assert summary["completed"] == 3
        assert summary["incomplete"] == 0

        async with db.session() as session:
            run = await EvaluationRunRepository(session).get(run_id=resumed_id)
        assert run.status is EvaluationRunStatus.COMPLETED

    async def test_duplicate_completion_is_prevented_not_duplicated(
        self,
        db,
        harness,  # noqa: ANN001
    ) -> None:
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=["t1"]
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.RATE_LIMITED, error_detail="HTTP 429"),
        )
        # Re-attempting the same task id overwrites, never duplicates.
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True),
        )

        from app.repositories.evaluation_repository import EvaluationTaskResultRepository

        async with db.session() as session:
            results = await EvaluationTaskResultRepository(session).list_for_run(run_id=run_id)
        assert len(results) == 1
        assert results[0].status is EvaluationTaskStatus.COMPLETED

    async def test_deterministic_task_ordering(self, db, harness) -> None:  # noqa: ANN001
        """Task results come back in insertion order, not database-arbitrary
        order -- a resumed run's "which tasks are left" logic must not
        depend on an unstable ordering.
        """
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        run_id = await tracker.start_or_resume(
            harness=harness,
            dataset_version="v1",
            mode="deterministic",
            task_ids=["t3", "t1", "t2"],
        )
        for task_id in ("t3", "t1", "t2"):
            await tracker.record_task(
                run_id=run_id,
                task_id=task_id,
                category="cat",
                outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True),
            )

        from app.repositories.evaluation_repository import EvaluationTaskResultRepository

        async with db.session() as session:
            results = await EvaluationTaskResultRepository(session).list_for_run(run_id=run_id)
        assert [r.task_id for r in results] == ["t3", "t1", "t2"]


class TestFinalizeSummary:
    async def test_infrastructure_failure_is_not_reported_as_a_model_failure(
        self,
        db,
        harness,  # noqa: ANN001
    ) -> None:
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        task_ids = ["t1", "t2"]
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="real_llm", task_ids=task_ids
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=False),
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t2",
            category="cat",
            outcome=TaskOutcome(status=EvaluationTaskStatus.ERRORED, error_detail="boom"),
        )
        summary = await tracker.finalize(run_id=run_id, task_ids=task_ids)
        # t1: a real model judgment failure (COMPLETED, passed=False).
        # t2: an infrastructure failure (ERRORED) -- distinct buckets.
        assert summary["completed"] == 1
        assert summary["passed"] == 0
        assert summary["errored"] == 1

    async def test_missing_metric_is_excluded_not_zeroed(self, db, harness) -> None:  # noqa: ANN001
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        task_ids = ["t1", "t2"]
        run_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="deterministic", task_ids=task_ids
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(
                status=EvaluationTaskStatus.COMPLETED, passed=True, metrics={"recall": 1.0}
            ),
        )
        await tracker.record_task(
            run_id=run_id,
            task_id="t2",
            category="cat",
            # No `recall` key at all -- must not be treated as 0.
            outcome=TaskOutcome(status=EvaluationTaskStatus.COMPLETED, passed=True, metrics={}),
        )
        summary = await tracker.finalize(run_id=run_id, task_ids=task_ids)
        assert summary["mean_recall"] == 1.0  # averaged over the one task that HAS it
        assert summary["mean_precision"] is None  # no task has it at all -- explicit N/A


class TestRegressionComparison:
    async def test_finds_the_immediately_preceding_completed_run(
        self,
        db,
        harness,  # noqa: ANN001
    ) -> None:
        tracker = EvaluationRunTracker(db, now_fn=lambda: JAN)
        first_id = await tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="deterministic", task_ids=["t1"]
        )
        await tracker.record_task(
            run_id=first_id,
            task_id="t1",
            category="cat",
            outcome=TaskOutcome(
                status=EvaluationTaskStatus.COMPLETED, passed=True, metrics={"recall": 1.0}
            ),
        )
        await tracker.finalize(run_id=first_id, task_ids=["t1"])

        later_tracker = EvaluationRunTracker(db, now_fn=lambda: JAN + timedelta(days=1))
        second_id = await later_tracker.start_or_resume(
            harness=harness, dataset_version="v1", mode="deterministic", task_ids=["t1"]
        )
        assert second_id != first_id  # the first run already COMPLETED -- a new run starts

        async with db.session() as session:
            previous = await EvaluationRunRepository(session).find_previous_completed(
                harness=harness,
                dataset_version="v1",
                mode="deterministic",
                before=JAN + timedelta(days=1),
            )
        assert previous is not None
        assert previous.id == first_id
