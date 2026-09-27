"""Phase 11 (M1) background-job correctness, against real Neo4j and PostgreSQL.

Each test pins one defect from docs/PHASE11_PLAN.md section 6.2:

* J1  a Neo4j outage while applying a candidate is retryable, never INVALID;
* J4  a worker that lost its lease cannot overwrite the new owner's result;
* J5  retry exhaustion is an explicit terminal state, visible on the target;
* J2  a crashed ingestion is recovered by the ingestion sweep;
* J6  an ingestion that crashed after its chunk jobs committed can retry
      (the FK on extraction_jobs.target_chunk_id no longer blocks it);
* J8/J9 a parse timeout is terminal, and a still-running parser thread
      blocks (defers) the next parse instead of running two at once;
* J11 the embedding backfill reaches every memory, not just the first page.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.errors import GraphUnavailableError, LLMUnavailableError
from app.documents import ingestion as ingestion_module
from app.documents.ingestion import run_ingestion_sweep_tick, run_ingestion_worker_tick
from app.embeddings.fake import FakeEmbeddingClient
from app.extraction.runner import ExtractionRunner, run_sweep_tick, run_worker_tick
from app.extraction.trigger import maybe_trigger_extraction
from app.graph.connection import GraphClient
from app.llm.fake import FakeLLMClient
from app.models.conversation import ExtractionStatus, MessageRole
from app.models.document import DocumentStatus
from app.models.extraction import (
    CandidateEntity,
    CandidateMemory,
    CandidateTemporal,
    DecisionType,
    ExtractionJobStatus,
    ExtractionResult,
    Span,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.extraction_queue import ExtractionQueue
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.semantic.embedding_pipeline import run_embedding_backfill_tick
from app.services.document_service import DocumentService
from app.services.memory_service import MemoryService
from app.storage.local import LocalDocumentStorage
from tests.conftest import JAN

pytestmark = pytest.mark.integration


class FlakyGraph:
    """Delegates to a real GraphClient, but the next `fail_writes` write calls
    raise `GraphUnavailableError` -- exactly what the driver-error mapper
    produces for ServiceUnavailable/SessionExpired (graph/connection.py)."""

    def __init__(self, real: GraphClient, *, fail_writes: int = 0) -> None:
        self._real = real
        self.fail_writes = fail_writes

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._real, name)

    def _maybe_fail(self) -> None:
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise GraphUnavailableError("simulated Neo4j outage")

    async def write(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self._maybe_fail()
        return await self._real.write(*args, **kwargs)

    async def write_transaction(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self._maybe_fail()
        return await self._real.write_transaction(*args, **kwargs)


def _settings(**overrides: object) -> Settings:
    return Settings(extraction_enabled=True, extraction_window_size=1, **overrides)


def _candidate(content: str, name: str) -> CandidateMemory:
    start = content.index(name)
    return CandidateMemory(
        statement=f"User uses {name}",
        memory_type="semantic",
        persistence="persistent",
        confidence=0.9,
        entities=[
            CandidateEntity(
                name=name,
                entity_type="technology",
                mention_span=Span(start=start, end=start + len(name)),
            )
        ],
        relationships=[],
        temporal=CandidateTemporal(),
        source_message_id="placeholder",
        source_span=Span(start=0, end=len(content)),
        reasoning="test fixture",
    )


async def _message_job(db, settings, user_id: str, content: str):  # noqa: ANN001, ANN202
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="t", now=JAN
        )
        msg_repo = MessageRepository(session, user_id=user_id)
        message = await msg_repo.append(
            message_id=str(uuid.uuid4()),
            conversation_id=conv.id,
            role=MessageRole.USER,
            content=content,
            token_count=None,
            now=JAN,
        )
        await maybe_trigger_extraction(
            message=message,
            message_repo=msg_repo,
            job_repo=ExtractionJobRepository(session, user_id=user_id),
            settings=settings,
            now=JAN,
        )
    return message


async def _job(db, user_id: str):  # noqa: ANN001, ANN202
    async with db.session() as session:
        jobs = await ExtractionJobRepository(session, user_id=user_id).list_recent(
            limit=5, offset=0
        )
    return jobs[0]


async def _memory_count(graph: GraphClient, user_id: str) -> int:
    rows = await graph.read(
        "MATCH (m:Memory {user_id: $user_id}) RETURN count(m) AS n", {"user_id": user_id}
    )
    return rows[0]["n"]


async def _tick(db, graph, settings, llm, *, now, worker_id="w"):  # noqa: ANN001, ANN002, ANN202
    return await run_worker_tick(
        db=db,
        graph_client=graph,
        llm_client=llm,
        embedding_client=FakeEmbeddingClient(),
        settings=settings,
        worker_id=worker_id,
        limit=5,
        now_fn=lambda: now,
    )


class TestNeo4jOutageIsRetryable:
    async def test_write_failure_is_retried_and_memory_stored_exactly_once(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        content = "I use Grafana at work every single day."
        await _message_job(db, settings, user_id, content)
        llm = FakeLLMClient(response=ExtractionResult(candidates=[_candidate(content, "Grafana")]))

        flaky = FlakyGraph(graph, fail_writes=1)
        await _tick(db, flaky, settings, llm, now=JAN)

        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.FAILED, "must stay retryable"
        assert job.error_code == "graph_unavailable"
        assert job.next_attempt_at is not None
        async with db.session() as session:
            records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
                extraction_job_id=job.id
            )
        assert records == [], "no candidate may be recorded (let alone INVALID)"
        assert await _memory_count(graph, user_id) == 0

        # Neo4j is back; retry after the backoff.
        await _tick(db, flaky, settings, llm, now=JAN + timedelta(hours=2))
        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.DONE
        async with db.session() as session:
            records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
                extraction_job_id=job.id
            )
        assert [r.decision for r in records] == [DecisionType.NEW]
        assert await _memory_count(graph, user_id) == 1

    async def test_failure_on_second_candidate_keeps_first_and_retries_second(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        content = "I use Grafana and Prometheus at work."
        await _message_job(db, settings, user_id, content)
        llm = FakeLLMClient(
            response=ExtractionResult(
                candidates=[_candidate(content, "Grafana"), _candidate(content, "Prometheus")]
            )
        )
        flaky = FlakyGraph(graph)
        # Let candidate 1's writes through, then fail the next write.
        calls = {"n": 0}
        real_wt = graph.write_transaction

        async def write_transaction(*a, **k):  # noqa: ANN002, ANN003, ANN202
            calls["n"] += 1
            if calls["n"] == 2:
                raise GraphUnavailableError("simulated")
            return await real_wt(*a, **k)

        flaky.write_transaction = write_transaction  # type: ignore[method-assign]
        await _tick(db, flaky, settings, llm, now=JAN)
        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.FAILED
        assert await _memory_count(graph, user_id) == 1

        await _tick(db, graph, settings, llm, now=JAN + timedelta(hours=2))
        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.DONE
        assert await _memory_count(graph, user_id) == 2, "each memory exactly once"


class TestStaleWorker:
    async def test_stale_worker_cannot_overwrite_new_owner(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        content = "I use Grafana at work every single day."
        await _message_job(db, settings, user_id, content)
        llm = FakeLLMClient(response=ExtractionResult(candidates=[_candidate(content, "Grafana")]))

        async with db.session() as session:
            (job_a,) = await ExtractionQueue(session).claim_batch(
                limit=1, worker_id="A:lease-1", now=JAN, max_attempts=3
            )
        # Worker A stalls past its lease; the sweep reclaims; worker B claims.
        later = JAN + timedelta(seconds=settings.extraction_lease_seconds + 5)
        async with db.session() as session:
            await ExtractionQueue(session).reclaim_expired_leases(
                lease_seconds=settings.extraction_lease_seconds, max_attempts=3, now=later
            )
        async with db.session() as session:
            (job_b,) = await ExtractionQueue(session).claim_batch(
                limit=1, worker_id="B:lease-2", now=later, max_attempts=3
            )
        runner = ExtractionRunner(
            graph_client=graph,
            llm_client=llm,
            embedding_client=FakeEmbeddingClient(),
            settings=settings,
            now_fn=lambda: later,
        )
        await runner.run(job_b, db=db)
        done = await _job(db, user_id)
        assert done.status is ExtractionJobStatus.DONE
        assert await _memory_count(graph, user_id) == 1

        # A wakes up: a direct completion write must match zero rows ...
        async with db.session() as session:
            overwrote = await ExtractionQueue(session).mark_failed(
                job_id=job_a.id,
                error_code="internal_error",
                error_detail="stale",
                permanent=True,
                next_attempt_at=None,
                now=later,
                owner="A:lease-1",
            )
        assert overwrote is False
        # ... and running its stale job object writes nothing at all.
        await runner.run(job_a, db=db)
        after = await _job(db, user_id)
        assert after.status is ExtractionJobStatus.DONE
        assert after.completed_at == done.completed_at
        assert await _memory_count(graph, user_id) == 1


class TestRetryExhaustion:
    async def test_last_failed_attempt_is_terminal_and_visible(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        settings = _settings(extraction_max_attempts=2)
        message = await _message_job(
            db, settings, user_id, "I use Grafana at work every single day."
        )
        llm = FakeLLMClient(failure=LLMUnavailableError("provider down"))

        await _tick(db, graph, settings, llm, now=JAN)
        assert (await _job(db, user_id)).status is ExtractionJobStatus.FAILED
        await _tick(db, graph, settings, llm, now=JAN + timedelta(hours=2))

        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.ABANDONED
        assert job.attempts == 2
        assert job.error_code == "llm_unavailable"
        assert job.error_detail.startswith("retries exhausted after 2 attempts")
        assert job.completed_at is not None and job.next_attempt_at is None
        async with db.session() as session:
            stored = await MessageRepository(session, user_id=user_id).get(message_id=message.id)
        assert stored.extraction_status is ExtractionStatus.FAILED

    async def test_sweep_resolves_legacy_exhausted_failed_jobs(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        settings = _settings(extraction_max_attempts=2)
        message = await _message_job(
            db, settings, user_id, "I use Grafana at work every single day."
        )
        job = await _job(db, user_id)
        # A row left behind by the pre-Phase-11 code: failed, no attempts left.
        async with db.session() as session:
            await session.execute(
                text(
                    "UPDATE extraction_jobs SET status='failed', attempts=2, "
                    "error_code='llm_unavailable', next_attempt_at=:t WHERE id=:id"
                ),
                {"t": JAN, "id": job.id},
            )
        counts = await run_sweep_tick(
            db=db,
            graph_client=graph,
            llm_client=FakeLLMClient(failure=LLMUnavailableError("x")),
            embedding_client=FakeEmbeddingClient(),
            settings=settings,
            now_fn=lambda: JAN + timedelta(days=1),
        )
        assert counts["abandoned_exhausted"] >= 1
        assert (await _job(db, user_id)).status is ExtractionJobStatus.ABANDONED
        async with db.session() as session:
            stored = await MessageRepository(session, user_id=user_id).get(message_id=message.id)
        assert stored.extraction_status is ExtractionStatus.FAILED


def _doc_body(tag: str) -> bytes:
    return "\n\n".join(
        f"Section {i} of {tag}. The Atlas project uses Postgres for storage." for i in range(4)
    ).encode()


async def _upload(db, storage, settings, user_id: str, tag: str):  # noqa: ANN001, ANN202
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
    async with db.session() as session:
        document, _ = await DocumentService(
            session, storage, settings=settings, now_fn=lambda: JAN
        ).upload_document(user_id=user_id, filename=f"{tag}.txt", content=_doc_body(tag))
    return document


async def _doc(db, user_id: str, document_id: str):  # noqa: ANN001, ANN202
    async with db.session() as session:
        return await DocumentRepository(session, user_id=user_id).get(document_id=document_id)


async def _ingestion_job_row(db, document_id: str):  # noqa: ANN001, ANN202
    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, attempts, error_code FROM ingestion_jobs WHERE document_id=:d"
                ),
                {"d": document_id},
            )
        ).one()
    return row


class TestIngestionRecovery:
    async def test_crash_during_parse_is_recovered_by_the_sweep(
        self,
        db,
        user_id,
        pg_cleanup,
        tmp_path: Path,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        storage = LocalDocumentStorage(root_dir=str(tmp_path))
        document = await _upload(db, storage, settings, user_id, "crash-parse")
        # A worker claims the job, moves the document to PARSING, and dies.
        async with db.session() as session:
            await session.execute(
                text(
                    "UPDATE ingestion_jobs SET status='in_progress', attempts=1, locked_at=:t, "
                    "locked_by='dead-worker:1' WHERE document_id=:d"
                ),
                {"t": JAN, "d": document.id},
            )
            await DocumentRepository(session, user_id=user_id).set_status(
                document_id=document.id, status=DocumentStatus.PARSING
            )

        later = JAN + timedelta(seconds=settings.ingestion_lease_seconds + 5)
        counts = await run_ingestion_sweep_tick(
            db=db, storage=storage, settings=settings, now_fn=lambda: later
        )
        assert counts["reclaimed_leases"] == 1
        assert counts["jobs_processed"] == 1
        assert (await _doc(db, user_id, document.id)).status is DocumentStatus.EXTRACTING
        assert (await _ingestion_job_row(db, document.id)).status == "done"

        # Idempotent: a second sweep changes nothing.
        again = await run_ingestion_sweep_tick(
            db=db, storage=storage, settings=settings, now_fn=lambda: later
        )
        assert again["reclaimed_leases"] == 0 and again["jobs_processed"] == 0

    async def test_retry_after_chunk_jobs_committed_is_not_blocked_by_fk(
        self,
        db,
        user_id,
        pg_cleanup,
        tmp_path: Path,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        storage = LocalDocumentStorage(root_dir=str(tmp_path))
        document = await _upload(db, storage, settings, user_id, "crash-after-jobs")
        await run_ingestion_worker_tick(
            db=db, storage=storage, settings=settings, worker_id="w", now_fn=lambda: JAN
        )
        async with db.session() as session:
            chunks_before = await DocumentChunkRepository(
                session, user_id=user_id
            ).list_for_document(document_id=document.id)
            jobs_before = (
                await session.execute(
                    text("SELECT count(*) FROM extraction_jobs WHERE document_id=:d"),
                    {"d": document.id},
                )
            ).scalar()
        assert chunks_before and jobs_before == len(chunks_before)

        # Simulate the crash window: chunks + chunk jobs committed, but the
        # worker died before mark_done. Pre-Phase-11 code deleted the chunks on
        # retry, which the FK from extraction_jobs refused -- forever.
        async with db.session() as session:
            await session.execute(
                text(
                    "UPDATE ingestion_jobs SET status='in_progress', locked_at=:t, "
                    "locked_by='dead-worker:2', completed_at=NULL WHERE document_id=:d"
                ),
                {"t": JAN, "d": document.id},
            )
        # And the raw bytes are gone (transient staging storage): resume must
        # not need them.
        await storage.delete(storage_key=document.storage_key, user_id=user_id)

        later = JAN + timedelta(seconds=settings.ingestion_lease_seconds + 5)
        await run_ingestion_sweep_tick(
            db=db, storage=storage, settings=settings, now_fn=lambda: later
        )

        assert (await _ingestion_job_row(db, document.id)).status == "done"
        async with db.session() as session:
            chunks_after = await DocumentChunkRepository(
                session, user_id=user_id
            ).list_for_document(document_id=document.id)
            jobs_after = (
                await session.execute(
                    text("SELECT count(*) FROM extraction_jobs WHERE document_id=:d"),
                    {"d": document.id},
                )
            ).scalar()
        assert [c.id for c in chunks_after] == [c.id for c in chunks_before]
        assert jobs_after == jobs_before, "chunk jobs are ensured idempotently, not duplicated"

    async def test_missing_bytes_before_ingestion_fail_the_document_explicitly(
        self,
        db,
        user_id,
        pg_cleanup,
        tmp_path: Path,  # noqa: ANN001
    ) -> None:
        settings = _settings()
        storage = LocalDocumentStorage(root_dir=str(tmp_path))
        document = await _upload(db, storage, settings, user_id, "bytes-gone")
        await storage.delete(storage_key=document.storage_key, user_id=user_id)
        await run_ingestion_worker_tick(
            db=db, storage=storage, settings=settings, worker_id="w", now_fn=lambda: JAN
        )
        doc = await _doc(db, user_id, document.id)
        assert doc.status is DocumentStatus.FAILED
        assert doc.error_code == "document_missing"
        assert (await _ingestion_job_row(db, document.id)).status == "abandoned"


class TestParseTimeout:
    async def test_timeout_is_terminal_and_a_running_parse_defers_the_next(
        self,
        db,
        user_id,
        pg_cleanup,
        tmp_path: Path,
        monkeypatch,  # noqa: ANN001
    ) -> None:
        settings = _settings(ingestion_parse_timeout_seconds=0.3)
        storage = LocalDocumentStorage(root_dir=str(tmp_path))
        slow = await _upload(db, storage, settings, user_id, "slow")
        real_parse = ingestion_module.parse_document

        def slow_parse(data, **kwargs):  # noqa: ANN001, ANN003, ANN202
            if b"slow" in data:
                time.sleep(1.5)
            return real_parse(data, **kwargs)

        monkeypatch.setattr(ingestion_module, "parse_document", slow_parse)
        await run_ingestion_worker_tick(
            db=db, storage=storage, settings=settings, worker_id="w", limit=1, now_fn=lambda: JAN
        )
        doc = await _doc(db, user_id, slow.id)
        assert doc.status is DocumentStatus.FAILED and doc.error_code == "parse_timeout"
        row = await _ingestion_job_row(db, slow.id)
        assert row.status == "abandoned", "timeout must be terminal, like the document"

        # The timed-out thread is still running: the next document must wait
        # (deferred, no attempt consumed) rather than start a second parser.
        fast = await _upload(db, storage, settings, user_id, "fast")
        await run_ingestion_worker_tick(
            db=db, storage=storage, settings=settings, worker_id="w", limit=1, now_fn=lambda: JAN
        )
        row = await _ingestion_job_row(db, fast.id)
        assert (row.status, row.attempts, row.error_code) == ("failed", 0, "parser_busy")

        time.sleep(1.5)  # the runaway parse finishes and frees the slot
        await run_ingestion_worker_tick(
            db=db,
            storage=storage,
            settings=settings,
            worker_id="w",
            limit=1,
            now_fn=lambda: JAN + timedelta(minutes=1),
        )
        assert (await _ingestion_job_row(db, fast.id)).status == "done"


class TestEmbeddingBackfill:
    async def test_backfill_reaches_every_memory_beyond_the_first_page(
        self,
        db,
        graph,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        settings = _settings(embedding_backfill_batch_size=50)
        memories = MemoryService(MemoryRepository(graph))
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        for i in range(230):
            await memories.create(
                user_id=user_id,
                payload=MemoryCreate.model_validate({"content": f"Backfill fact {i}"}),
            )
        await memories.create(
            user_id=other_user_id,
            payload=MemoryCreate.model_validate({"content": "Someone else's fact"}),
        )
        embeddings = FakeEmbeddingClient(dimension=settings.embedding_dimension)

        total, ticks, complete = 0, 0, 0
        while not complete:
            counts = await run_embedding_backfill_tick(
                db=db,
                graph_client=graph,
                embedding_client=embeddings,
                settings=settings,
                user_id=user_id,
            )
            total += counts["embedded"]
            complete = counts["complete"]
            ticks += 1
            assert ticks < 20, "backfill must make forward progress"
        assert total == 230

        async with db.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT count(*), count(DISTINCT memory_id) FROM memory_embeddings "
                        "WHERE user_id=:u"
                    ),
                    {"u": user_id},
                )
            ).one()
            others = (
                await session.execute(
                    text("SELECT count(*) FROM memory_embeddings WHERE user_id=:u"),
                    {"u": other_user_id},
                )
            ).scalar()
        assert tuple(rows) == (230, 230), "every memory exactly once"
        assert others == 0, "user isolation: another user's memories are untouched"

        again = await run_embedding_backfill_tick(
            db=db,
            graph_client=graph,
            embedding_client=embeddings,
            settings=settings,
            user_id=user_id,
        )
        assert again["embedded"] == 0 and again["complete"] == 1
