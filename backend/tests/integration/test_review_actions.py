"""The review workflow's write side (ADR 0010, PHASE4_PLAN.md sections
6/9/10/11/15), against real Neo4j and real PostgreSQL.

Reuses the same scenario builders as test_review_service.py to produce genuine
review candidates, then exercises every `ReviewActionService` action: the
happy path, optimistic-concurrency (stale `if_version` -> 409), idempotent
repeats (a second identical call is a no-op, not a duplicate mutation), and
cross-user isolation (another user can never act on someone else's candidate).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.embeddings.fake import FakeEmbeddingClient
from app.extraction.runner import run_worker_tick
from app.extraction.trigger import maybe_trigger_extraction
from app.llm.fake import FakeLLMClient
from app.models.conversation import MessageRole
from app.models.enums import MemoryStatus
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
from app.services.review_action_service import ReviewActionService
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


@pytest.fixture
def memory_service(graph) -> MemoryService:  # noqa: ANN001
    return MemoryService(MemoryRepository(graph))


@pytest.fixture
def entity_repo(graph) -> EntityRepository:  # noqa: ANN001
    return EntityRepository(graph)


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
            message=message, message_repo=msg_repo, job_repo=job_repo, settings=settings, now=now
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


def _services(session, graph):  # noqa: ANN001, ANN202
    review_service = ReviewService(
        session=session,
        memory_service=MemoryService(MemoryRepository(graph)),
        entity_repository=EntityRepository(graph),
    )
    action_service = ReviewActionService(
        session=session,
        review_service=review_service,
        memory_repository=MemoryRepository(graph),
        entity_repository=EntityRepository(graph),
    )
    return review_service, action_service


class TestConfirmDuplicate:
    async def test_happy_path_idempotent_repeat_and_stale_version(
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
            worker_id="ra-1",
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
            worker_id="ra-2",
        )
        assert [r.decision for r in records] == [DecisionType.LIKELY_DUPLICATE]
        new_memory_id = records[0].memory_id

        async with db.session() as session:
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_memory_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version

            # Cross-user isolation: another user cannot act on this candidate.
            with pytest.raises(NotFoundError):
                await action_service.confirm_duplicate(
                    user_id=other_user_id,
                    candidate_id=candidate_id,
                    if_version=version,
                    reason=None,
                )

            # Stale version is rejected, never silently applied.
            with pytest.raises(ConflictError):
                await action_service.confirm_duplicate(
                    user_id=user_id, candidate_id=candidate_id, if_version=version + 99, reason=None
                )

            detail = await action_service.confirm_duplicate(
                user_id=user_id,
                candidate_id=candidate_id,
                if_version=version,
                reason="near-identical restatement",
            )
            assert detail.status == "confirmed_duplicate"
            assert detail.memory_a.memory.id == new_memory_id
            assert detail.memory_a.memory.status is MemoryStatus.MERGED
            assert detail.memory_a.duplicate_of == [detail.memory_b.memory.id]
            assert detail.memory_b.memory.status is MemoryStatus.ACTIVE

            # Idempotent repeat with the ORIGINAL (now-stale) version still
            # succeeds, because the row already carries exactly this outcome.
            repeat = await action_service.confirm_duplicate(
                user_id=user_id, candidate_id=candidate_id, if_version=version, reason=None
            )
            assert repeat.status == "confirmed_duplicate"
            assert repeat.version == detail.version, "must not apply a second mutation"

            history = await review_service.list_audit_log(user_id=user_id, limit=10, offset=0)
            confirm_entries = [e for e in history.items if e.action == "CONFIRM_DUPLICATE"]
            assert len(confirm_entries) == 1, (
                "the idempotent repeat must not add a second audit row"
            )


class TestConfirmDistinct:
    async def test_confirm_distinct_never_touches_neo4j(
        self,
        db,
        graph,
        settings,
        user_id,
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
            worker_id="rd-1",
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
            worker_id="rd-2",
        )
        new_memory_id = records[0].memory_id

        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_memory_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version

            detail = await action_service.confirm_distinct(
                user_id=user_id, candidate_id=candidate_id, if_version=version, reason="unrelated"
            )
            assert detail.status == "confirmed_distinct"
            assert detail.memory_a.memory.id == new_memory_id
            assert detail.memory_a.memory.status is MemoryStatus.ACTIVE, "must be untouched"
            assert detail.memory_a.duplicate_of == []


class TestResolveConflict:
    async def _make_conflicting_pair(self, db, graph, settings, user_id, conversation_id):  # noqa: ANN001
        c1 = "The Drawing Extractor project uses EasyOCR for extraction."
        records1 = await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c1,
            candidates=[
                candidate(
                    statement="Drawing Extractor uses EasyOCR",
                    entities=[
                        CandidateEntity(
                            name="Drawing Extractor",
                            entity_type="project",
                            mention_span=span(c1, "Drawing Extractor"),
                        ),
                        CandidateEntity(
                            name="EasyOCR",
                            entity_type="technology",
                            mention_span=span(c1, "EasyOCR"),
                        ),
                    ],
                    relationships=[
                        CandidateRelationship(
                            source="Drawing Extractor", predicate="ocr_engine", target="EasyOCR"
                        )
                    ],
                    source_span=Span(start=0, end=len(c1)),
                )
            ],
            now=JAN,
            worker_id="rc-1",
        )
        old_memory_id = records1[0].memory_id

        c2 = "Also the project uses Tesseract for extraction."
        records2 = await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c2,
            candidates=[
                candidate(
                    statement="Drawing Extractor uses Tesseract",
                    entities=[
                        CandidateEntity(name="Drawing Extractor", entity_type="project"),
                        CandidateEntity(
                            name="Tesseract",
                            entity_type="technology",
                            mention_span=span(c2, "Tesseract"),
                        ),
                    ],
                    relationships=[
                        CandidateRelationship(
                            source="Drawing Extractor", predicate="ocr_engine", target="Tesseract"
                        )
                    ],
                    source_span=Span(start=0, end=len(c2)),
                )
            ],
            now=JAN + timedelta(minutes=1),
            worker_id="rc-2",
        )
        new_memory_id = records2[0].memory_id
        return old_memory_id, new_memory_id

    async def test_supersede_requires_a_valid_winner_and_flips_statuses(
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
        old_id, new_id = await self._make_conflicting_pair(
            db, graph, settings, user_id, conversation_id
        )
        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_memory_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version

            with pytest.raises(ValidationError):
                await action_service.resolve_conflict(
                    user_id=user_id,
                    candidate_id=candidate_id,
                    if_version=version,
                    resolution="supersede",
                    winner_memory_id="not-a-real-memory-id",
                    reason=None,
                )

            detail = await action_service.resolve_conflict(
                user_id=user_id,
                candidate_id=candidate_id,
                if_version=version,
                resolution="supersede",
                winner_memory_id=new_id,
                reason="Tesseract replaced EasyOCR",
            )
            assert detail.status == "conflict_resolved"

        winner = await memory_service.get(user_id=user_id, memory_id=new_id)
        loser = await memory_service.get(user_id=user_id, memory_id=old_id)
        assert winner.memory.status is MemoryStatus.ACTIVE
        assert loser.memory.status is MemoryStatus.SUPERSEDED
        assert winner.supersedes == [old_id]

    async def test_keep_both_leaves_neo4j_untouched(
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
        old_id, new_id = await self._make_conflicting_pair(
            db, graph, settings, user_id, conversation_id
        )
        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_memory_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version
            detail = await action_service.resolve_conflict(
                user_id=user_id,
                candidate_id=candidate_id,
                if_version=version,
                resolution="keep_both",
                winner_memory_id=None,
                reason="both genuinely used concurrently",
            )
            assert detail.status == "conflict_resolved"

        winner = await memory_service.get(user_id=user_id, memory_id=new_id)
        loser = await memory_service.get(user_id=user_id, memory_id=old_id)
        assert winner.memory.status is MemoryStatus.CONFLICTING, "left exactly as it was"
        assert loser.memory.status is MemoryStatus.ACTIVE

    async def test_reject_returns_the_flagged_memory_to_active(
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
        old_id, new_id = await self._make_conflicting_pair(
            db, graph, settings, user_id, conversation_id
        )
        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_memory_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version
            await action_service.resolve_conflict(
                user_id=user_id,
                candidate_id=candidate_id,
                if_version=version,
                resolution="reject",
                winner_memory_id=None,
                reason="classifier error, not a real conflict",
            )

        new_detail = await memory_service.get(user_id=user_id, memory_id=new_id)
        old_detail = await memory_service.get(user_id=user_id, memory_id=old_id)
        assert new_detail.memory.status is MemoryStatus.ACTIVE
        assert old_detail.memory.status is MemoryStatus.ACTIVE


class TestEntityMergeActions:
    async def _make_ambiguous_pair(self, db, graph, settings, user_id, conversation_id):  # noqa: ANN001
        c1_text = "We evaluated OpenAI for the classification pipeline."
        await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c1_text,
            candidates=[
                candidate(
                    statement="Evaluated OpenAI for classification",
                    entities=[
                        CandidateEntity(
                            name="OpenAI",
                            entity_type="company",
                            mention_span=span(c1_text, "OpenAI"),
                        )
                    ],
                    source_span=Span(start=0, end=len(c1_text)),
                )
            ],
            now=JAN,
            worker_id="rm-1",
        )
        now2 = JAN + timedelta(seconds=1)
        c2_text = "Open AI was also considered as an alternative."
        await _turn(
            db,
            graph,
            settings,
            user_id=user_id,
            conversation_id=conversation_id,
            content=c2_text,
            candidates=[
                candidate(
                    statement="Open AI was considered as an alternative",
                    entities=[
                        CandidateEntity(
                            name="Open AI",
                            entity_type="company",
                            mention_span=span(c2_text, "Open AI"),
                        )
                    ],
                    source_span=Span(start=0, end=len(c2_text)),
                )
            ],
            now=now2,
            worker_id="rm-2",
        )

    async def test_approve_resolves_pending_placeholder_and_merges(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        retrieval,
        entity_repo,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        await self._make_ambiguous_pair(db, graph, settings, user_id, conversation_id)

        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_entity_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version

            detail_before = await review_service.get_entity_candidate(
                user_id=user_id, candidate_id=candidate_id
            )
            assert detail_before.entity_a is not None  # "OpenAI", already real
            # "Open AI" was flagged via a `pending:<name>` placeholder (it did
            # not exist yet at flag time), but both turns have completed by
            # now, so the read side already resolves it to the real entity.
            assert detail_before.entity_b is not None
            assert detail_before.entity_b.entity.name == "Open AI"

            open_ai = await retrieval.find_entity_by_name(user_id=user_id, name="Open AI")

            with pytest.raises(ValidationError):
                await action_service.approve_entity_merge(
                    user_id=user_id,
                    candidate_id=candidate_id,
                    if_version=version,
                    canonical_entity_id="not-a-real-entity-id",
                    reason=None,
                )

            detail = await action_service.approve_entity_merge(
                user_id=user_id,
                candidate_id=candidate_id,
                if_version=version,
                canonical_entity_id=open_ai.id,
                reason="same company",
            )
            assert detail.status == "merged"
            assert detail.entity_a is not None
            assert detail.entity_b is not None
            assert open_ai.id in (detail.entity_a.entity.id, detail.entity_b.entity.id)

            resolved_openai_no_space = await entity_repo.resolve(
                user_id=user_id, entity_id=detail_before.entity_a.entity.id
            )
            assert resolved_openai_no_space.entity.id == open_ai.id
            assert resolved_openai_no_space.was_redirected is True

    async def test_reject_leaves_both_entities_distinct(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        retrieval,
        entity_repo,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        await self._make_ambiguous_pair(db, graph, settings, user_id, conversation_id)

        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_entity_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version
            detail = await action_service.reject_entity_merge(
                user_id=user_id, candidate_id=candidate_id, if_version=version, reason="distinct"
            )
            assert detail.status == "rejected"

        openai = await retrieval.find_entity_by_name(user_id=user_id, name="OpenAI")
        open_ai = await retrieval.find_entity_by_name(user_id=user_id, name="Open AI")
        assert openai.id != open_ai.id

        resolved = await entity_repo.resolve(user_id=user_id, entity_id=openai.id)
        assert resolved.was_redirected is False

    async def test_reverse_undoes_a_previously_approved_merge(
        self,
        db,
        graph,
        settings,
        user_id,
        conversation_id,
        retrieval,
        entity_repo,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        await self._make_ambiguous_pair(db, graph, settings, user_id, conversation_id)

        # Captured BEFORE the merge: `find_entity_by_name`/`get_entity` now
        # transparently redirect (this same Phase 4 fix), so the raw,
        # pre-redirect "OpenAI" id must be captured while it still resolves
        # to itself, not re-looked-up afterward.
        openai_raw = await entity_repo.find_by_normalized_name(
            user_id=user_id, normalized_name="openai"
        )

        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            listing = await review_service.list_entity_candidates(
                user_id=user_id, limit=10, offset=0
            )
            candidate_id = listing.items[0].id
            version = listing.items[0].version
            open_ai = await retrieval.find_entity_by_name(user_id=user_id, name="Open AI")
            merged_detail = await action_service.approve_entity_merge(
                user_id=user_id,
                candidate_id=candidate_id,
                if_version=version,
                canonical_entity_id=open_ai.id,
                reason=None,
            )
            merged_version = merged_detail.version

        resolved_before = await entity_repo.resolve(user_id=user_id, entity_id=openai_raw.id)
        assert resolved_before.was_redirected is True
        assert resolved_before.entity.id == open_ai.id

        # The redirect is also transparent through the ordinary lookup paths.
        via_get_entity = await retrieval.get_entity(user_id=user_id, entity_id=openai_raw.id)
        assert via_get_entity.entity.id == open_ai.id
        via_name = await retrieval.find_entity_by_name(user_id=user_id, name="OpenAI")
        assert via_name.id == open_ai.id

        async with db.session() as session:
            review_service, action_service = _services(session, graph)
            reversed_detail = await action_service.reverse_entity_merge(
                user_id=user_id, candidate_id=candidate_id, if_version=merged_version, reason=None
            )
            assert reversed_detail.status == "reversed"

        resolved_after = await entity_repo.resolve(user_id=user_id, entity_id=openai_raw.id)
        assert resolved_after.was_redirected is False
        assert resolved_after.entity.id == openai_raw.id
        via_name_after = await retrieval.find_entity_by_name(user_id=user_id, name="OpenAI")
        assert via_name_after.id == openai_raw.id
