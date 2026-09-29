"""Semantic duplicate/conflict signals and the entity-resolution embedding
enhancement (ADR 0009, PHASE3_PLAN.md sections 5-7) -- against real Neo4j and
real PostgreSQL, using `FakeEmbeddingClient` so no API key or network access is
needed.

Formalizes the scenarios manually verified during implementation: a
differently-predicated near-identical restatement is flagged (never merged), a
weaker match is FLAGGED rather than LIKELY_DUPLICATE, an unrelated statement
stays plain NEW, and embedding-based entity-name similarity is recorded as a
review candidate without ever merging two entities.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

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
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService
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
async def conversation_id(db, user_id: str) -> str:  # noqa: ANN001
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="t", now=JAN
        )
        return conv.id


@pytest.fixture
def memory_service(graph) -> MemoryService:  # noqa: ANN001
    return MemoryService(MemoryRepository(graph))


@pytest.fixture
def retrieval(graph) -> RetrievalService:  # noqa: ANN001
    return RetrievalService(EntityRepository(graph), max_hops=4)


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
        jobs = await job_repo.list_recent(limit=10, offset=0)
        target_job = next(j for j in jobs if j.target_message_id == message.id)
        records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
            extraction_job_id=target_job.id
        )
    return records


@pytest.fixture
async def settings():  # noqa: ANN201
    from app.core.config import Settings

    return Settings(extraction_enabled=True, extraction_window_size=6)


class TestSemanticDuplicateDetection:
    async def test_differently_predicated_near_identical_restatement_is_likely_duplicate(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        memory_service,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """ "Drawing Extractor uses Gemini..." then "...utilizes Gemini..." for a
        near-identical statement: exact-triple matching cannot catch this (the
        predicate differs), semantic evidence must.
        """
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
            worker_id="sd-1",
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
            worker_id="sd-2",
        )

        assert [r.decision for r in records] == [DecisionType.LIKELY_DUPLICATE]
        assert records[0].memory_id is not None

        # Never auto-merged: two distinct memories must exist.
        result = await memory_service.search(user_id=user_id, limit=50, offset=0)
        assert result.total == 2

        # PHASE4_PLAN.md section 6: LIKELY_DUPLICATE is also recorded as a
        # MemoryReviewCandidate, carrying the actual similarity score as
        # evidence -- never just prose in decision_reason.
        async with db.session() as session:
            review_flagged = await MemoryReviewCandidateRepository(
                session, user_id=user_id
            ).list_unreviewed(limit=10, offset=0)
        assert len(review_flagged) == 1
        review_row = review_flagged[0]
        assert review_row["kind"] == "likely_duplicate"
        assert review_row["memory_id_a"] == records[0].memory_id
        assert review_row["score"] is not None
        assert 0.0 < review_row["score"] <= 1.0
        assert review_row["source_extraction_candidate_id"] == records[0].id
        assert review_row["status"] == "unreviewed"

    async def test_unrelated_statement_stays_plain_new(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        memory_service,
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
            worker_id="sd-3",
        )

        now2 = JAN + timedelta(seconds=1)
        c2_text = "The weather has been unusually cold this week in the mountains."
        c2 = candidate(
            statement="The weather has been unusually cold this week in the mountains",
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
            worker_id="sd-4",
        )
        assert [r.decision for r in records] == [DecisionType.NEW]

    async def test_bare_statement_duplicate_is_caught_by_semantic_similarity(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        memory_service,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """docs/PHASE2_PLAN.md's own stated limitation, closed here: a
        relationship-less candidate restating an existing bare statement in
        near-identical words must not be classified NEW forever.
        """
        c1_text = "I really enjoy working on early morning hiking in the mountains."
        c1 = candidate(
            statement="User enjoys early morning hikes in the mountains",
            entities=[
                CandidateEntity(
                    name="Hiking", entity_type="concept", mention_span=span(c1_text, "hiking")
                )
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
            worker_id="sd-5",
        )

        now2 = JAN + timedelta(seconds=1)
        c2_text = "Just confirming again: I really enjoy early morning hiking in the mountains."
        # Near-identical, not identical: since Phase 14 an IDENTICAL statement is an
        # exact DUPLICATE (see the next test); this one exercises the semantic path.
        c2 = candidate(
            statement="User enjoys early morning hikes in mountains",
            entities=[
                CandidateEntity(
                    name="Hiking", entity_type="concept", mention_span=span(c2_text, "hiking")
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
            worker_id="sd-6",
        )
        assert records[0].decision in (DecisionType.LIKELY_DUPLICATE, DecisionType.FLAGGED)

    async def test_an_identical_bare_statement_is_an_exact_duplicate(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        memory_service,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """Phase 14 (M7): identical text corroborates the existing memory
        (DUPLICATE, a second source) instead of a second active copy."""
        statement = "User enjoys early morning hikes in the mountains"
        records = []
        for i, text_ in enumerate(
            [
                "I really enjoy working on early morning hiking in the mountains.",
                "Just confirming again: I really enjoy early morning hiking in the mountains.",
            ]
        ):
            c = candidate(
                statement=statement,
                entities=[
                    CandidateEntity(
                        name="Hiking", entity_type="concept", mention_span=span(text_, "hiking")
                    )
                ],
                source_span=Span(start=0, end=len(text_)),
            )
            records = await _turn(
                db, graph, settings, user_id=user_id, conversation_id=conversation_id,
                content=text_, candidates=[c], now=JAN + timedelta(seconds=i),
                worker_id=f"sd-exact-{i}",
            )  # fmt: skip
        assert records[0].decision is DecisionType.DUPLICATE
        found = await memory_service.search(user_id=user_id, query="early morning hikes")
        assert found.total == 1


class TestEntityResolutionEmbeddingEnhancement:
    async def test_similar_entity_names_are_flagged_never_merged(
        self,
        db,
        graph,
        user_id,
        conversation_id,
        retrieval,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """ "Gemini Pro" and "Gemini Pro Model" share most of their words (a real
        embedding-similarity case the canonical-key check -- exact after
        stripping punctuation/whitespace -- cannot catch, since the strings
        differ by more than punctuation). `FakeEmbeddingClient`'s bag-of-words
        model puts their cosine similarity at ~0.82 (2 of 3 words shared) --
        genuinely high, but below the default 0.85 threshold, so this test uses
        a locally-lowered threshold rather than the shared `settings` fixture,
        to test the mechanism deterministically without depending on incidental
        hash-bucket luck at the default threshold.
        """
        from app.core.config import Settings

        settings = Settings(
            extraction_enabled=True, extraction_window_size=6, semantic_entity_flag_threshold=0.8
        )
        c1_text = "The project uses Gemini Pro for classification."
        c1 = candidate(
            statement="Drawing Extractor uses Gemini Pro",
            entities=[
                CandidateEntity(
                    name="Gemini Pro",
                    entity_type="technology",
                    mention_span=span(c1_text, "Gemini Pro"),
                )
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
            worker_id="er-1",
        )

        now2 = JAN + timedelta(seconds=1)
        c2_text = "The project also uses Gemini Pro Model for another task."
        c2 = candidate(
            statement="Drawing Extractor uses Gemini Pro Model",
            entities=[
                CandidateEntity(
                    name="Gemini Pro Model",
                    entity_type="technology",
                    mention_span=span(c2_text, "Gemini Pro Model"),
                )
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
            worker_id="er-2",
        )

        # Both entities must exist, independently -- never merged.
        gemini_pro = await retrieval.find_entity_by_name(user_id=user_id, name="Gemini Pro")
        gemini_pro_model = await retrieval.find_entity_by_name(
            user_id=user_id, name="Gemini Pro Model"
        )
        assert gemini_pro.id != gemini_pro_model.id

        async with db.session() as session:
            from sqlalchemy import select

            from app.db.models import EntityMergeCandidate as MergeRow

            rows = (
                (await session.execute(select(MergeRow).where(MergeRow.user_id == user_id)))
                .scalars()
                .all()
            )
        embedding_flags = [r for r in rows if r.evidence.get("signal") == "embedding"]
        assert len(embedding_flags) >= 1, (
            "expected an embedding-similarity flag for Gemini Pro / Gemini Pro Model"
        )
        assert embedding_flags[0].status == "unreviewed"
        assert embedding_flags[0].score is not None
