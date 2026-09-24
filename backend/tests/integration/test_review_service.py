"""The review workflow's read side (ADR 0010, PHASE4_PLAN.md sections 6/12),
against real Neo4j and real PostgreSQL.

Reuses the same scenario builders as test_extraction_runner.py /
test_semantic_duplicate_detection.py to produce genuine review candidates
(a LIKELY_DUPLICATE memory pair and an ambiguous OpenAI/Open AI entity pair),
then exercises `ReviewService` end to end -- including that one user can never
list or fetch another user's review candidates.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.errors import NotFoundError
from app.embeddings.fake import FakeEmbeddingClient
from app.extraction.runner import run_worker_tick
from app.extraction.trigger import maybe_trigger_extraction
from app.llm.fake import FakeLLMClient
from app.models.conversation import MessageRole
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
from app.repositories.entity_repository import EntityRepository
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.memory_service import MemoryService
from app.services.review_service import ReviewService
from tests.conftest import JAN

pytestmark = pytest.mark.integration


def span(content: str, needle: str) -> Span:
    start = content.index(needle)
    return Span(start=start, end=start + len(needle))


def candidate(
    *,
    statement: str,
    entities: list[CandidateEntity] | None = None,
    relationships: list[CandidateRelationship] | None = None,
    message_id: str = "placeholder",
    source_span: Span,
) -> CandidateMemory:
    return CandidateMemory(
        statement=statement,
        memory_type="semantic",
        persistence="persistent",
        confidence=0.9,
        entities=entities or [],
        relationships=relationships or [],
        temporal=CandidateTemporal(),
        source_message_id=message_id,
        source_span=source_span,
        reasoning="test fixture",
    )


@pytest.fixture
async def settings():  # noqa: ANN201
    from app.core.config import Settings

    return Settings(extraction_enabled=True, extraction_window_size=6)


@pytest.fixture
async def conversation_id(db, user_id: str) -> str:  # noqa: ANN001
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="t", now=JAN
        )
        return conv.id


async def _turn(
    db,
    graph,
    settings,
    *,
    user_id: str,
    conversation_id: str,  # noqa: ANN001
    content: str,
    candidates: list[CandidateMemory],
    now,
    worker_id: str,
):
    async with db.session() as session:
        msg_repo = MessageRepository(session, user_id=user_id)
        job_repo = ExtractionJobRepository(session, user_id=user_id)
        message = await msg_repo.append(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
            token_count=None,
            now=now,
        )
        await maybe_trigger_extraction(
            message=message,
            message_repo=msg_repo,
            job_repo=job_repo,
            settings=settings,
            now=now,
        )
    llm = FakeLLMClient(response=ExtractionResult(candidates=candidates))
    await run_worker_tick(
        db=db,
        graph_client=graph,
        llm_client=llm,
        embedding_client=FakeEmbeddingClient(),
        settings=settings,
        worker_id=worker_id,
        limit=5,
        now_fn=lambda: now,
    )
    async with db.session() as session:
        job_repo = ExtractionJobRepository(session, user_id=user_id)
        jobs = await job_repo.list_recent(limit=1, offset=0)
        job = jobs[0]
        records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
            extraction_job_id=job.id
        )
    return records


class TestMemoryReviewCandidates:
    async def test_likely_duplicate_is_listed_and_readable_in_full(
        self,
        db,
        graph,
        settings,
        user_id,
        other_user_id,
        conversation_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        c1_text = "The project Drawing Extractor uses Gemini for classification of images."
        c1 = candidate(
            statement="Drawing Extractor uses Gemini for classification of images",
            entities=[
                CandidateEntity(
                    name="Drawing Extractor",
                    entity_type="project",
                    mention_span=span(c1_text, "Drawing Extractor"),
                ),
                CandidateEntity(
                    name="Gemini", entity_type="technology", mention_span=span(c1_text, "Gemini")
                ),
            ],
            relationships=[
                CandidateRelationship(source="Drawing Extractor", predicate="uses", target="Gemini")
            ],
            source_span=Span(start=0, end=len(c1_text)),
        )
        await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c1_text,
            candidates=[c1],
            now=JAN,
            worker_id="rs-1",
        )

        now2 = JAN + timedelta(seconds=1)
        c2_text = "The project Drawing Extractor utilizes Gemini for classification of images."
        c2 = candidate(
            statement="Drawing Extractor uses Gemini for classification of images",
            entities=[
                CandidateEntity(
                    name="Drawing Extractor",
                    entity_type="project",
                    mention_span=span(c2_text, "Drawing Extractor"),
                ),
                CandidateEntity(
                    name="Gemini", entity_type="technology", mention_span=span(c2_text, "Gemini")
                ),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="utilizes", target="Gemini"
                )
            ],
            source_span=Span(start=0, end=len(c2_text)),
        )
        records = await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c2_text,
            candidates=[c2],
            now=now2,
            worker_id="rs-2",
        )
        assert [r.decision for r in records] == [DecisionType.LIKELY_DUPLICATE]

        async with db.session() as session:
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)

        async with db.session() as session:
            service = ReviewService(
                session=session,
                memory_service=MemoryService(MemoryRepository(graph)),
                entity_repository=EntityRepository(graph),
            )
            listing = await service.list_memory_candidates(user_id=user_id, limit=10, offset=0)
            assert listing.total == 1
            assert len(listing.items) == 1
            candidate_id = listing.items[0].id
            assert listing.items[0].kind == "likely_duplicate"

            detail = await service.get_memory_candidate(user_id=user_id, candidate_id=candidate_id)
            assert detail.memory_a.memory.id == records[0].memory_id
            assert detail.memory_b.memory.content  # the earlier memory, resolved in full
            assert detail.score is not None

            # Cross-user isolation: another user must see nothing and get 404 on direct fetch.
            other_listing = await service.list_memory_candidates(
                user_id=other_user_id, limit=10, offset=0
            )
            assert other_listing.total == 0
            with pytest.raises(NotFoundError):
                await service.get_memory_candidate(user_id=other_user_id, candidate_id=candidate_id)

            history = await service.list_audit_log(user_id=user_id, limit=10, offset=0)
            assert history.total == 0, "no write actions have happened yet in this step"


class TestEntityMergeCandidates:
    async def test_ambiguous_entity_pair_is_listed_and_readable_in_full(
        self,
        db,
        graph,
        settings,
        user_id,
        other_user_id,
        conversation_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        c1_text = "We evaluated OpenAI for the classification pipeline."
        c1 = candidate(
            statement="Evaluated OpenAI for classification",
            entities=[
                CandidateEntity(
                    name="OpenAI", entity_type="company", mention_span=span(c1_text, "OpenAI")
                ),
            ],
            source_span=Span(start=0, end=len(c1_text)),
        )
        await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c1_text,
            candidates=[c1],
            now=JAN,
            worker_id="rs-e1",
        )

        now2 = JAN + timedelta(seconds=1)
        c2_text = "Open AI was also considered as an alternative."
        c2 = candidate(
            statement="Open AI was considered as an alternative",
            entities=[
                CandidateEntity(
                    name="Open AI", entity_type="company", mention_span=span(c2_text, "Open AI")
                ),
            ],
            source_span=Span(start=0, end=len(c2_text)),
        )
        await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c2_text,
            candidates=[c2],
            now=now2,
            worker_id="rs-e2",
        )

        async with db.session() as session:
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
            service = ReviewService(
                session=session,
                memory_service=MemoryService(MemoryRepository(graph)),
                entity_repository=EntityRepository(graph),
            )
            listing = await service.list_entity_candidates(user_id=user_id, limit=10, offset=0)
            assert listing.total == 1
            candidate_id = listing.items[0].id

            detail = await service.get_entity_candidate(user_id=user_id, candidate_id=candidate_id)
            # entity_a is the already-existing "OpenAI"; entity_b was flagged
            # via a `pending:<name>` placeholder ("Open AI" did not exist in
            # Neo4j yet when ambiguity detection ran -- persistence creates it
            # right after). By the time this review read runs, both turns have
            # completed, so `resolve_pending_entity_id` finds "Open AI" by its
            # now-real normalized name -- the placeholder does not stay
            # unresolved forever, which is exactly what an approve action
            # needs (see test_review_actions.py).
            assert detail.entity_a is not None
            assert detail.entity_a.entity.name == "OpenAI"
            assert detail.entity_b is not None
            assert detail.entity_b.entity.name == "Open AI"
            assert {detail.evidence["existing_name"], detail.evidence["candidate_name"]} == {
                "OpenAI",
                "Open AI",
            }
            assert detail.status == "unreviewed"

            other_listing = await service.list_entity_candidates(
                user_id=other_user_id, limit=10, offset=0
            )
            assert other_listing.total == 0
            with pytest.raises(NotFoundError):
                await service.get_entity_candidate(user_id=other_user_id, candidate_id=candidate_id)
