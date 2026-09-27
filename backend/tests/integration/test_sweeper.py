"""Phase 11 (M2) in-process sweeper, against real Neo4j and PostgreSQL.

* a restart does not lose queued work: pending jobs and jobs a dead process
  held `in_progress` are completed by the sweeper after boot;
* only one sweeper runs a pass at a time (advisory lock);
* graceful shutdown waits for the in-flight pass, and a pass cancelled at the
  grace deadline leaves nothing half-written (the job is recovered later);
* one failing step does not stop the others, and failures back off;
* dead sessions are cleaned up without touching live ones;
* the real timed loop picks up new work on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.embeddings.fake import FakeEmbeddingClient
from app.llm.fake import FakeLLMClient
from app.models.extraction import ExtractionJobStatus, ExtractionResult
from app.repositories.extraction_queue import ExtractionQueue
from app.storage.local import LocalDocumentStorage
from app.workers.sweeper import SWEEP_ADVISORY_LOCK_KEY, Sweeper
from tests.conftest import JAN
from tests.integration.test_job_reliability import (
    _candidate,
    _job,
    _memory_count,
    _message_job,
)

pytestmark = pytest.mark.integration

CONTENT = "I use Grafana at work every single day."


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "extraction_enabled": True,
        "extraction_window_size": 1,
        "sweep_interval_seconds": 0.2,
        "sweep_initial_delay_seconds": 0.0,
        "sweep_shutdown_grace_seconds": 2.0,
    }
    base.update(overrides)
    return Settings(**base)


def _sweeper(db, graph, settings, llm, tmp_path, *, now_fn=None) -> Sweeper:  # noqa: ANN001
    kwargs = {"now_fn": now_fn} if now_fn else {}
    return Sweeper(
        db=db,
        graph_client=graph,
        llm_client_fn=lambda: llm,
        embedding_client_fn=lambda: FakeEmbeddingClient(),
        storage_fn=lambda: LocalDocumentStorage(root_dir=str(tmp_path)),
        settings=settings,
        **kwargs,
    )


def _extracting_llm() -> FakeLLMClient:
    return FakeLLMClient(response=ExtractionResult(candidates=[_candidate(CONTENT, "Grafana")]))


class TestRestartRecovery:
    async def test_restart_does_not_lose_queued_or_in_flight_work(
        self,
        db,
        graph,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,
        tmp_path,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        await _message_job(db, settings, user_id, CONTENT)  # in flight at "crash"
        await _message_job(db, settings, other_user_id, CONTENT)  # still pending
        # Process 1 claims the first job and dies without completing it.
        async with db.session() as session:
            claimed = await ExtractionQueue(session).claim_batch(
                limit=1, worker_id="process-1:dead", now=JAN, max_attempts=3
            )
        assert len(claimed) == 1

        # Process 2 boots; its sweeper runs after the dead lease expired.
        later = JAN + timedelta(seconds=settings.extraction_lease_seconds + 5)
        sweeper = _sweeper(db, graph, settings, _extracting_llm(), tmp_path, now_fn=lambda: later)
        counts = await sweeper.run_once()
        assert counts is not None
        assert counts["extraction"]["reclaimed_leases"] >= 1

        for uid in (user_id, other_user_id):
            assert (await _job(db, uid)).status is ExtractionJobStatus.DONE
            assert await _memory_count(graph, uid) == 1


class TestSingleSweeper:
    async def test_pass_is_skipped_while_another_instance_holds_the_lock(
        self,
        db,
        graph,
        tmp_path,  # noqa: ANN001
    ) -> None:
        sweeper = _sweeper(db, graph, _settings(), _extracting_llm(), tmp_path)
        async with db.session() as other_instance:
            held = (
                await other_instance.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SWEEP_ADVISORY_LOCK_KEY}
                )
            ).scalar()
            assert held
            assert await sweeper.run_once() is None
        assert sweeper.stats.skipped_locked == 1
        # Released with the other transaction: the next pass runs.
        assert await sweeper.run_once() is not None


class TestShutdown:
    async def test_stop_waits_for_the_pass_or_cancels_at_the_grace_deadline(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,
        tmp_path,  # noqa: ANN001
    ) -> None:
        settings = _settings(sweep_shutdown_grace_seconds=0.3)
        await _message_job(db, settings, user_id, CONTENT)
        entered = asyncio.Event()

        async def slow(prompt: str):  # noqa: ANN202, ARG001
            entered.set()
            await asyncio.sleep(30)

        class SlowLLM(FakeLLMClient):
            async def complete_structured(self, **kwargs):  # noqa: ANN003, ANN201
                await slow(kwargs["prompt"])

        sweeper = _sweeper(db, graph, settings, SlowLLM(), tmp_path)
        sweeper.start()
        await asyncio.wait_for(entered.wait(), timeout=10)
        await asyncio.wait_for(sweeper.stop(), timeout=5)
        assert not sweeper.running

        # Cancelled mid-job: nothing written, the job is simply still leased ...
        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.IN_PROGRESS
        assert await _memory_count(graph, user_id) == 0
        # ... and a later sweeper recovers it once the lease expires.
        # The first sweeper leased on the real clock; move past its lease.
        later = datetime.now(UTC) + timedelta(days=1)
        recovering = _sweeper(
            db, graph, settings, _extracting_llm(), tmp_path, now_fn=lambda: later
        )
        await recovering.run_once()
        assert (await _job(db, user_id)).status is ExtractionJobStatus.DONE
        assert await _memory_count(graph, user_id) == 1

    async def test_stop_is_prompt_when_idle(self, db, graph, tmp_path) -> None:  # noqa: ANN001
        sweeper = _sweeper(
            db, graph, _settings(sweep_interval_seconds=30), FakeLLMClient(), tmp_path
        )
        sweeper.start()
        await asyncio.sleep(0.2)
        await asyncio.wait_for(sweeper.stop(), timeout=1)


class TestStepIsolation:
    async def test_a_failing_step_does_not_stop_the_others(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,
        tmp_path,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        await _message_job(db, settings, user_id, CONTENT)

        def broken_storage():  # noqa: ANN202
            raise RuntimeError("storage misconfigured")

        sweeper = Sweeper(
            db=db,
            graph_client=graph,
            llm_client_fn=_extracting_llm,
            embedding_client_fn=lambda: FakeEmbeddingClient(),
            storage_fn=broken_storage,
            settings=settings,
        )
        counts = await sweeper.run_once()
        assert "ingestion" not in counts and "sessions" in counts
        assert (await _job(db, user_id)).status is ExtractionJobStatus.DONE
        assert sweeper.stats.consecutive_failures == 1
        assert sweeper.stats.last_error == "ingestion: RuntimeError"


class TestSessionCleanup:
    async def test_dead_sessions_are_deleted_and_live_ones_kept(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        tmp_path,  # noqa: ANN001
    ) -> None:
        now = JAN + timedelta(days=60)
        async with db.session() as session:
            await session.execute(
                text("INSERT INTO users (id, created_at) VALUES (:u, :t)"), {"u": user_id, "t": JAN}
            )
            rows = {
                "revoked_long_ago": (JAN, now + timedelta(days=1), now, JAN + timedelta(days=1)),
                "expired_long_ago": (JAN, JAN + timedelta(days=7), JAN, None),
                "idle_long_ago": (JAN, now + timedelta(days=5), JAN + timedelta(days=2), None),
                "live": (now, now + timedelta(days=7), now, None),
                "revoked_yesterday": (now, now + timedelta(days=7), now, now - timedelta(days=1)),
            }
            for label, (created, expires, used, revoked) in rows.items():
                await session.execute(
                    text(
                        "INSERT INTO auth_sessions (id, user_id, token_sha256, created_at, "
                        "expires_at, last_used_at, revoked_at) VALUES "
                        "(:id, :u, :h, :c, :e, :l, :r)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "u": user_id,
                        "h": hashlib.sha256(label.encode() + user_id.encode()).hexdigest(),
                        "c": created,
                        "e": expires,
                        "l": used,
                        "r": revoked,
                    },
                )
        sweeper = _sweeper(db, graph, _settings(), FakeLLMClient(), tmp_path, now_fn=lambda: now)
        counts = await sweeper.run_once()
        assert counts["sessions"]["deleted"] >= 3
        async with db.session() as session:
            remaining = (
                await session.execute(
                    text("SELECT count(*) FROM auth_sessions WHERE user_id = :u"), {"u": user_id}
                )
            ).scalar()
        assert remaining == 2, "the live session and the recently revoked one are kept"


class TestTimedLoop:
    async def test_running_loop_processes_new_work_by_itself(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,
        tmp_path,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        sweeper = _sweeper(db, graph, settings, _extracting_llm(), tmp_path)
        sweeper.start()
        try:
            await _message_job(db, settings, user_id, CONTENT)
            # The job can reach DONE mid-pass; `passes` counts only when the
            # whole pass (extraction, ingestion, sessions) has finished.
            for _ in range(50):
                done = (await _job(db, user_id)).status is ExtractionJobStatus.DONE
                if done and sweeper.stats.passes >= 1:
                    break
                await asyncio.sleep(0.1)
            assert (await _job(db, user_id)).status is ExtractionJobStatus.DONE
            assert sweeper.stats.passes >= 1
        finally:
            await sweeper.stop()
