"""`propose_memory_update` (docs/PHASE5_PLAN.md section 7), against real
Neo4j and real PostgreSQL. This is the highest-risk tool: the only one that
writes. Every test here proves the write goes through the exact same
`ExtractionRunner._apply_candidate` pipeline passive extraction uses --
nothing here re-implements grounding, duplicate/conflict detection, or
entity resolution -- and that the audit trail (`extraction_jobs`/
`extraction_candidates`) records it exactly like any other memory write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.agent.tools import ToolDependencies, ToolStatus, propose_memory_update
from app.core.config import Settings
from app.embeddings.fake import FakeEmbeddingClient
from app.llm.fake import FakeLLMClient
from app.models.agent import ProposeMemoryUpdateArgs
from app.models.conversation import Message, MessageRole
from app.models.extraction import CandidateEntity, CandidateRelationship, CandidateTemporal
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.hybrid_retrieval_service import HybridRetrievalService
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService

pytestmark = pytest.mark.integration

JAN = datetime(2026, 1, 15, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(embedding_provider="fake", agent_write_enabled=True, retrieval_top_k=10)


async def _seed_conversation(db, user_id: str) -> str:  # noqa: ANN001
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="agent write test", now=JAN
        )
        return conv.id


async def _append_message(db, user_id: str, conversation_id: str, content: str) -> Message:  # noqa: ANN001
    async with db.session() as session:
        return await MessageRepository(session, user_id=user_id).append(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
            token_count=None,
            now=JAN,
        )


def _deps(
    db,  # noqa: ANN001
    graph,  # noqa: ANN001
    settings: Settings,
    *,
    conversation_id: str,
    target_message: Message,
    window_text: str,
) -> ToolDependencies:
    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
    retrieval_service = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=lambda: JAN)
    embedding_client = FakeEmbeddingClient(dimension=settings.embedding_dimension)
    hybrid = HybridRetrievalService(
        session=None,
        memory_service=memory_service,
        retrieval_service=retrieval_service,
        embedding_client=embedding_client,
        settings=settings,
        now_fn=lambda: JAN,
    )
    return ToolDependencies(
        hybrid_retrieval=hybrid,
        retrieval=retrieval_service,
        memory=memory_service,
        provenance=ProvenanceService(None, memory_service),
        settings=settings,
        now_fn=lambda: JAN,
        db=db,
        graph_client=graph,
        llm_client=FakeLLMClient(response="unused -- _apply_candidate never calls the LLM"),
        embedding_client=embedding_client,
        conversation_id=conversation_id,
        target_message=target_message,
        window_text=window_text,
    )


def _args(
    *,
    statement: str,
    entities: list[CandidateEntity],
    relationships: list[CandidateRelationship] | None = None,
    temporal: CandidateTemporal | None = None,
    persistence: str = "persistent",
    confidence: float = 0.9,
) -> ProposeMemoryUpdateArgs:
    return ProposeMemoryUpdateArgs(
        statement=statement,
        memory_type="semantic",
        persistence=persistence,
        confidence=confidence,
        entities=entities,
        relationships=relationships or [],
        temporal=temporal or CandidateTemporal(),
        reasoning="agent proposal for test",
    )


async def _latest_candidate_records(db, user_id: str):  # noqa: ANN001, ANN201
    async with db.session() as session:
        jobs = await ExtractionJobRepository(session, user_id=user_id).list_recent(
            limit=1, offset=0
        )
        job = jobs[0]
        return await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
            extraction_job_id=job.id
        )


class TestSuccessfulProposal:
    async def test_new_fact_is_written_through_the_real_pipeline(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses PaddleOCR for OCR."
        message = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=message,
            window_text=content,
        )
        args = _args(
            statement="Drawing Extractor uses PaddleOCR",
            entities=[
                CandidateEntity(name="Drawing Extractor", entity_type="project"),
                CandidateEntity(name="PaddleOCR", entity_type="technology"),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                )
            ],
        )

        result = await propose_memory_update(args, user_id=user_id, deps=deps)

        assert result.status == ToolStatus.OK
        assert result.data["decision"] == "NEW"
        memory_id = result.data["memory_id"]
        assert result.memory_ids == [memory_id]

        detail = await deps.memory.get(user_id=user_id, memory_id=memory_id)
        assert detail.memory.content == "Drawing Extractor uses PaddleOCR"

        # The write is audited exactly like passive extraction's own writes.
        records = await _latest_candidate_records(db, user_id)
        assert len(records) == 1
        assert records[0].memory_id == memory_id
        assert records[0].source_message_id == message.id
        assert records[0].decision.value == "NEW"


class TestDuplicateProposal:
    async def test_restating_an_active_fact_attaches_no_new_memory(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses EasyOCR."
        first = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=first,
            window_text=content,
        )
        seed_args = _args(
            statement="Drawing Extractor uses EasyOCR",
            entities=[
                CandidateEntity(name="Drawing Extractor", entity_type="project"),
                CandidateEntity(name="EasyOCR", entity_type="technology"),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="uses", target="EasyOCR"
                )
            ],
        )
        seeded = await propose_memory_update(seed_args, user_id=user_id, deps=deps)
        assert seeded.data["decision"] == "NEW"

        content2 = "Drawing Extractor still uses EasyOCR."
        second = await _append_message(db, user_id, conversation_id, content2)
        deps2 = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=second,
            window_text=content2,
        )
        result = await propose_memory_update(seed_args, user_id=user_id, deps=deps2)

        assert result.status == ToolStatus.OK
        assert result.data["decision"] == "DUPLICATE"
        assert result.data["memory_id"] == seeded.data["memory_id"], (
            "a duplicate restatement must attach to the SAME memory, never create a new one"
        )


class TestConflictingProposal:
    async def test_a_different_active_fact_with_no_change_signal_is_flagged_conflicting(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses EasyOCR."
        first = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=first,
            window_text=content,
        )
        await propose_memory_update(
            _args(
                statement="Drawing Extractor uses EasyOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="EasyOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="ocr_engine", target="EasyOCR"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps,
        )

        content2 = "Actually, Drawing Extractor uses PaddleOCR."
        second = await _append_message(db, user_id, conversation_id, content2)
        deps2 = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=second,
            window_text=content2,
        )
        result = await propose_memory_update(
            _args(
                statement="Drawing Extractor uses PaddleOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="PaddleOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="ocr_engine", target="PaddleOCR"
                    )
                ],
                # No is_change signal -- an unsignalled contradiction, never
                # blindly overwritten (CLAUDE.md section 14).
            ),
            user_id=user_id,
            deps=deps2,
        )

        assert result.status == ToolStatus.OK
        assert result.data["decision"] == "CONFLICTING"
        assert result.data["memory_id"] is not None


class TestTemporalReplacement:
    async def test_a_signalled_change_supersedes_the_old_fact(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses EasyOCR."
        first = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=first,
            window_text=content,
        )
        old = await propose_memory_update(
            _args(
                statement="Drawing Extractor uses EasyOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="EasyOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="uses", target="EasyOCR"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps,
        )

        content2 = "Drawing Extractor: we switched from EasyOCR to PaddleOCR."
        second = await _append_message(db, user_id, conversation_id, content2)
        deps2 = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=second,
            window_text=content2,
        )
        result = await propose_memory_update(
            _args(
                statement="Drawing Extractor switched from EasyOCR to PaddleOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="PaddleOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                    )
                ],
                temporal=CandidateTemporal(is_change=True, replaces_hint="EasyOCR"),
            ),
            user_id=user_id,
            deps=deps2,
        )

        assert result.status == ToolStatus.OK
        assert result.data["decision"] == "SUPERSEDING"
        new_id = result.data["memory_id"]

        old_detail = await deps.memory.get(user_id=user_id, memory_id=old.data["memory_id"])
        assert old_detail.memory.status.value == "superseded"
        new_detail = await deps.memory.get(user_id=user_id, memory_id=new_id)
        assert new_detail.memory.status.value == "active"


class TestProvenancePreservation:
    async def test_the_real_source_message_is_recorded_and_retrievable(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses PaddleOCR."
        message = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=message,
            window_text=content,
        )
        result = await propose_memory_update(
            _args(
                statement="Drawing Extractor uses PaddleOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="PaddleOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps,
        )

        async with db.session() as session:
            provenance = ProvenanceService(session, deps.memory)
            view = await provenance.get_provenance(
                user_id=user_id, memory_id=result.data["memory_id"]
            )
        assert len(view.sources) == 1
        assert view.sources[0].message is not None
        assert view.sources[0].message.id == message.id
        assert view.sources[0].message.content == content


class TestEntityResolution:
    async def test_a_differently_cased_mention_attaches_to_the_same_entity(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses EasyOCR."
        first = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=first,
            window_text=content,
        )
        await propose_memory_update(
            _args(
                statement="Drawing Extractor uses EasyOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="EasyOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="uses", target="EasyOCR"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps,
        )
        project = await deps.retrieval.find_entity_by_name(
            user_id=user_id, name="Drawing Extractor"
        )

        content2 = "The drawing extractor also uses Gemini."
        second = await _append_message(db, user_id, conversation_id, content2)
        deps2 = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=second,
            window_text=content2,
        )
        result = await propose_memory_update(
            _args(
                statement="drawing extractor uses Gemini",
                entities=[
                    CandidateEntity(name="drawing extractor", entity_type="project"),
                    CandidateEntity(name="Gemini", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="drawing extractor", predicate="uses", target="Gemini"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps2,
        )
        assert result.status == ToolStatus.OK
        detail = await deps.retrieval.get_entity(user_id=user_id, entity_id=project.id)
        assert detail.memory_count == 2, "both memories must resolve to the SAME entity node"


class TestRejectedProposal:
    async def test_non_persistent_proposal_is_rejected_without_writing(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Let me check that real quick."
        message = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=message,
            window_text=content,
        )
        result = await propose_memory_update(
            _args(statement="checking something", entities=[], persistence="temporary"),
            user_id=user_id,
            deps=deps,
        )
        assert result.status == ToolStatus.REJECTED
        assert result.data["decision"] == "IGNORE"
        assert result.data["memory_id"] is None

    async def test_an_ungrounded_entity_mention_is_rejected_as_invalid(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Nothing interesting was said here."
        message = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=message,
            window_text=content,
        )
        result = await propose_memory_update(
            _args(
                statement="Zzyzx Nonexistent Corp uses QuantumOCR",
                entities=[
                    CandidateEntity(name="Zzyzx Nonexistent Corp", entity_type="company"),
                    CandidateEntity(name="QuantumOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Zzyzx Nonexistent Corp", predicate="uses", target="QuantumOCR"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps,
        )
        assert result.status == ToolStatus.REJECTED
        assert result.data["decision"] == "INVALID"
        assert result.data["memory_id"] is None


class TestIdempotency:
    async def test_repeating_the_exact_same_proposal_writes_only_once(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses PaddleOCR."
        message = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            settings,
            conversation_id=conversation_id,
            target_message=message,
            window_text=content,
        )
        args = _args(
            statement="Drawing Extractor uses PaddleOCR",
            entities=[
                CandidateEntity(name="Drawing Extractor", entity_type="project"),
                CandidateEntity(name="PaddleOCR", entity_type="technology"),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                )
            ],
        )

        first = await propose_memory_update(args, user_id=user_id, deps=deps)
        second = await propose_memory_update(args, user_id=user_id, deps=deps)

        assert first.data["memory_id"] == second.data["memory_id"]
        assert second.data.get("idempotent") is True

        records = await _latest_candidate_records(db, user_id)
        assert len(records) == 1, "a repeated identical proposal must not double-write"


class TestCrossUserIsolation:
    async def test_two_users_proposing_the_same_statement_do_not_collide(
        self,
        db,
        graph,
        settings,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        conv_a = await _seed_conversation(db, user_id)
        conv_b = await _seed_conversation(db, other_user_id)
        content = "Drawing Extractor uses PaddleOCR."
        msg_a = await _append_message(db, user_id, conv_a, content)
        msg_b = await _append_message(db, other_user_id, conv_b, content)

        args = _args(
            statement="Drawing Extractor uses PaddleOCR",
            entities=[
                CandidateEntity(name="Drawing Extractor", entity_type="project"),
                CandidateEntity(name="PaddleOCR", entity_type="technology"),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                )
            ],
        )
        deps_a = _deps(
            db, graph, settings, conversation_id=conv_a, target_message=msg_a, window_text=content
        )
        deps_b = _deps(
            db, graph, settings, conversation_id=conv_b, target_message=msg_b, window_text=content
        )

        result_a = await propose_memory_update(args, user_id=user_id, deps=deps_a)
        result_b = await propose_memory_update(args, user_id=other_user_id, deps=deps_b)

        assert result_a.data["decision"] == "NEW"
        assert result_b.data["decision"] == "NEW", "the second user's own graph has no prior fact"
        assert result_a.data["memory_id"] != result_b.data["memory_id"]

        with pytest.raises(Exception):  # noqa: B017, PT011
            await deps_b.memory.get(user_id=other_user_id, memory_id=result_a.data["memory_id"])


class TestWritesDisabled:
    async def test_agent_write_disabled_rejects_without_touching_neo4j(
        self,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        disabled_settings = Settings(embedding_provider="fake", agent_write_enabled=False)
        conversation_id = await _seed_conversation(db, user_id)
        content = "Drawing Extractor uses PaddleOCR."
        message = await _append_message(db, user_id, conversation_id, content)
        deps = _deps(
            db,
            graph,
            disabled_settings,
            conversation_id=conversation_id,
            target_message=message,
            window_text=content,
        )

        result = await propose_memory_update(
            _args(
                statement="Drawing Extractor uses PaddleOCR",
                entities=[
                    CandidateEntity(name="Drawing Extractor", entity_type="project"),
                    CandidateEntity(name="PaddleOCR", entity_type="technology"),
                ],
                relationships=[
                    CandidateRelationship(
                        source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                    )
                ],
            ),
            user_id=user_id,
            deps=deps,
        )

        assert result.status == ToolStatus.REJECTED
        assert result.data == {}
        with pytest.raises(Exception):  # noqa: B017, PT011
            await deps.retrieval.find_entity_by_name(user_id=user_id, name="Drawing Extractor")
