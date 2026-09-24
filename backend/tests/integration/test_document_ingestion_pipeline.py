"""The document ingestion + extraction pipeline end to end, against real
Neo4j and real PostgreSQL, plus local disk (Phase 6, PHASE6_PLAN.md section
9; ADR 0012).

Exercises the full chain a real upload triggers: `LocalDocumentStorage` ->
`IngestionRunner` (validate/parse/chunk, `app/documents/ingestion.py`) ->
`ExtractionRunner` (now widened to process `source_kind='document_chunk'`
jobs, `app/extraction/runner.py`) -> the unmodified Phase 1 `MemoryService`.
Uses `FakeLLMClient` so no API key or network access is needed -- only the
two databases and local disk (`tmp_path`), matching the zero-cost testing
convention `test_extraction_runner.py` already established.

`TestConversationPathIsUnchanged` is the explicit regression guard the
Phase 6 directive asked for: it pins the exact `Source.reference`/`location`
shape a MESSAGE-sourced memory has always had, so a future change to the
now-shared `_persist`/`_apply_candidate` code cannot silently alter
conversation extraction's behavior without failing a test.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.core.config import Settings
from app.documents.ingestion import run_ingestion_worker_tick
from app.embeddings.fake import FakeEmbeddingClient
from app.extraction.runner import run_worker_tick
from app.extraction.trigger import maybe_trigger_extraction
from app.llm.fake import FakeLLMClient
from app.models.conversation import MessageRole
from app.models.document import DocumentStatus
from app.models.enums import SourceType
from app.models.extraction import (
    CandidateEntity,
    CandidateMemory,
    CandidateRelationship,
    CandidateTemporal,
    DecisionType,
    ExtractionResult,
    Span,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.repositories.ingestion_repository import IngestionJobRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.memory_service import MemoryService
from app.storage.local import LocalDocumentStorage
from tests.conftest import JAN

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path):  # noqa: ANN001, ANN201
    return LocalDocumentStorage(root_dir=str(tmp_path))


@pytest.fixture
def settings() -> Settings:
    return Settings(extraction_enabled=True, extraction_window_size=6)


async def _upload_document(
    db,  # noqa: ANN001
    storage,  # noqa: ANN001
    *,
    user_id: str,
    content: bytes,
    now,  # noqa: ANN001
) -> tuple[str, str]:
    """Store bytes, insert the `Document` row, queue its `IngestionJob`.

    Returns `(document_id, ingestion_job_id)`.
    """
    put_result = await storage.put(data=content, user_id=user_id)
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=now)
        document, _ = await DocumentRepository(session, user_id=user_id).create(
            document_id=str(uuid.uuid4()),
            filename="notes.txt",
            content_type="text/plain",
            size_bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            storage_key=put_result.storage_key,
            now=now,
        )
        job = await IngestionJobRepository(session, user_id=user_id).create_job(
            job_id=str(uuid.uuid4()), document_id=document.id, now=now
        )
    return document.id, job.id


class TestFullDocumentPipeline:
    async def test_upload_through_memory_creation(
        self,
        db,
        graph,
        storage,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        # Short enough to produce exactly one chunk at the default
        # `document_chunk_size_chars` (2000) -- keeps this test's LLM script
        # simple: one chunk, one job, one candidate.
        content = (
            b"Drawing Extractor is a project that uses PaddleOCR for text "
            b"recognition in scanned drawings."
        )
        document_id, _ = await _upload_document(
            db, storage, user_id=user_id, content=content, now=JAN
        )

        ingested = await run_ingestion_worker_tick(
            db=db,
            storage=storage,
            settings=settings,
            worker_id="itest-ingest",
            now_fn=lambda: JAN,
        )
        assert ingested == 1

        async with db.session() as session:
            document = await DocumentRepository(session, user_id=user_id).get(
                document_id=document_id
            )
            chunks = await DocumentChunkRepository(session, user_id=user_id).list_for_document(
                document_id=document_id
            )
        assert document.status is DocumentStatus.EXTRACTING
        assert len(chunks) == 1
        target_chunk = chunks[0]
        assert target_chunk.content == content.decode()

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=user_id).list_recent(
                limit=10, offset=0
            )
        chunk_jobs = [j for j in jobs if j.document_id == document_id]
        assert len(chunk_jobs) == 1
        assert chunk_jobs[0].source_kind.value == "document_chunk"
        assert chunk_jobs[0].target_chunk_id == target_chunk.id

        candidate = CandidateMemory(
            statement="Drawing Extractor uses PaddleOCR",
            memory_type="semantic",
            persistence="persistent",
            confidence=0.9,
            entities=[
                CandidateEntity(name="Drawing Extractor", entity_type="project"),
                CandidateEntity(name="PaddleOCR", entity_type="technology"),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                )
            ],
            temporal=CandidateTemporal(),
            # Reused as-is for a chunk id (ADR 0012): the LLM contract makes
            # no schema distinction between a message id and a chunk id here.
            source_message_id=target_chunk.id,
            source_span=Span(start=0, end=len(target_chunk.content)),
            reasoning="test fixture",
        )
        llm = FakeLLMClient(response=ExtractionResult(candidates=[candidate]))
        extracted = await run_worker_tick(
            db=db,
            graph_client=graph,
            llm_client=llm,
            embedding_client=FakeEmbeddingClient(),
            settings=settings,
            worker_id="itest-extract",
            limit=10,
            now_fn=lambda: JAN,
        )
        assert extracted == 1

        async with db.session() as session:
            records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
                extraction_job_id=chunk_jobs[0].id
            )
            chunk_after = await DocumentChunkRepository(session, user_id=user_id).get(
                chunk_id=target_chunk.id
            )
            document_after = await DocumentRepository(session, user_id=user_id).get(
                document_id=document_id
            )
        assert [r.decision for r in records] == [DecisionType.NEW]
        assert records[0].source_chunk_id == target_chunk.id
        assert records[0].source_message_id is None
        assert chunk_after.extraction_status.value == "done"
        # Every chunk of this document reached a terminal extraction_status
        # (there is only the one), so the document rolls up to COMPLETE
        # (app.extraction.runner.ExtractionRunner._maybe_finalize_document).
        assert document_after.status is DocumentStatus.COMPLETE

        memory_service = MemoryService(MemoryRepository(graph))
        detail = await memory_service.get(user_id=user_id, memory_id=records[0].memory_id)
        assert detail.memory.content == "Drawing Extractor uses PaddleOCR"
        assert len(detail.sources) == 1
        source = detail.sources[0]
        assert source.source_type is SourceType.DOCUMENT
        # `reference` is the document id (not the chunk id) -- a document has
        # no per-chunk identity of its own the way a message does, so the
        # chunk is threaded through `location` instead (PHASE6_PLAN.md
        # section 4, `ExtractionRunner._persist`'s `source_location_prefix`).
        assert source.reference == document_id
        assert source.location == f"chunk:{target_chunk.id}:0:{len(target_chunk.content)}"


class TestConversationPathIsUnchanged:
    """Regression guard: `_apply_candidate`/`_persist` now serve both the
    conversation and document paths (ADR 0012) -- this pins that a
    MESSAGE-sourced memory's `Source` is byte-for-byte what it was before
    Phase 6, with no `location` prefix and `reference` equal to the message
    id, exactly as every pre-Phase-6 test in test_extraction_runner.py
    already (implicitly) requires by continuing to pass unmodified.
    """

    async def test_message_source_has_no_chunk_prefix(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            conversation = await ConversationRepository(session, user_id=user_id).create(
                conversation_id=str(uuid.uuid4()), title="t", now=JAN
            )
            msg_repo = MessageRepository(session, user_id=user_id)
            job_repo = ExtractionJobRepository(session, user_id=user_id)
            content = "I am building a project called Drawing Extractor."
            message = await msg_repo.append(
                message_id=str(uuid.uuid4()),
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content=content,
                token_count=None,
                now=JAN,
            )
            await maybe_trigger_extraction(
                message=message,
                message_repo=msg_repo,
                job_repo=job_repo,
                settings=settings,
                now=JAN,
            )

        candidate = CandidateMemory(
            statement="User is building a project called Drawing Extractor",
            memory_type="semantic",
            persistence="persistent",
            confidence=0.9,
            entities=[CandidateEntity(name="Drawing Extractor", entity_type="project")],
            relationships=[],
            temporal=CandidateTemporal(),
            source_message_id=message.id,
            source_span=Span(start=0, end=len(content)),
            reasoning="test fixture",
        )
        llm = FakeLLMClient(response=ExtractionResult(candidates=[candidate]))
        await run_worker_tick(
            db=db,
            graph_client=graph,
            llm_client=llm,
            embedding_client=FakeEmbeddingClient(),
            settings=settings,
            worker_id="itest-conv-regression",
            limit=5,
            now_fn=lambda: JAN,
        )

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=user_id).list_recent(
                limit=1, offset=0
            )
            records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
                extraction_job_id=jobs[0].id
            )
        assert jobs[0].source_kind.value == "message"
        assert records[0].source_message_id == message.id
        assert records[0].source_chunk_id is None

        memory_service = MemoryService(MemoryRepository(graph))
        detail = await memory_service.get(user_id=user_id, memory_id=records[0].memory_id)
        source = detail.sources[0]
        assert source.source_type is SourceType.MESSAGE
        assert source.reference == message.id
        # No "chunk:" prefix -- exactly `"{start}:{end}"`, unchanged from
        # before Phase 6.
        assert source.location == f"0:{len(content)}"
