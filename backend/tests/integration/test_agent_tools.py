"""Phase 5 agent memory tools (docs/PHASE5_PLAN.md section 3), against real
Neo4j and real PostgreSQL. Each tool wraps an existing service -- these tests
prove the wrapping is correct, bounded, and user-isolated, not that the
underlying services work (that is Phases 1-4's own test suites).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.agent.tools import (
    ToolDependencies,
    ToolStatus,
    get_entity,
    get_entity_state,
    get_memory_history,
    get_source,
    search_memory,
    traverse_graph,
)
from app.core.config import Settings
from app.embeddings.fake import FakeEmbeddingClient
from app.models.agent import (
    GetEntityArgs,
    GetEntityStateArgs,
    GetMemoryHistoryArgs,
    GetSourceArgs,
    SearchMemoryArgs,
    TraverseGraphArgs,
)
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.hybrid_retrieval_service import HybridRetrievalService
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService

pytestmark = pytest.mark.integration

JAN = datetime(2026, 1, 15, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(embedding_provider="fake", retrieval_top_k=10)


def _deps(session, graph, settings, *, now_fn=lambda: JAN) -> ToolDependencies:  # noqa: ANN001
    """Built fresh inside each `async with db.session() as session:` block,
    matching every other integration test's pattern -- never a session
    constructed outside its own `async with` scope.
    """
    memory_service = MemoryService(MemoryRepository(graph), now_fn=now_fn)
    retrieval_service = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=now_fn)
    hybrid = HybridRetrievalService(
        session=session,
        memory_service=memory_service,
        retrieval_service=retrieval_service,
        embedding_client=FakeEmbeddingClient(dimension=settings.embedding_dimension),
        settings=settings,
        now_fn=now_fn,
    )
    provenance_service = ProvenanceService(session, memory_service)
    return ToolDependencies(
        hybrid_retrieval=hybrid,
        retrieval=retrieval_service,
        memory=memory_service,
        provenance=provenance_service,
        settings=settings,
        now_fn=now_fn,
    )


class TestSearchMemoryTool:
    async def test_finds_a_memory_and_reports_bounded_compact_shape(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Zzyzx Corp is our OCR vendor",
                    "entities": [{"name": "Zzyzx Corp", "entity_type": "company"}],
                }
            ),
        )
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await search_memory(
                SearchMemoryArgs(query="What do we know about Zzyzx Corp?"),
                user_id=user_id,
                deps=deps,
            )
        assert result.status == ToolStatus.OK
        assert created.memory.id in result.memory_ids
        memory_data = next(
            m for m in result.data["memories"] if m["memory_id"] == created.memory.id
        )
        assert set(memory_data.keys()) <= {
            "memory_id",
            "content",
            "entities",
            "temporal_state",
            "confidence",
            "status",
            "valid_from",
            "valid_until",
            "duplicate_of",
            "conflicts_with",
            "found_via",
        }
        assert memory_data["temporal_state"] == "current"

    async def test_no_match_returns_not_found_status_not_an_error(
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
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await search_memory(
                SearchMemoryArgs(query="something totally unrelated xyzzyplugh"),
                user_id=user_id,
                deps=deps,
            )
        assert result.status == ToolStatus.NOT_FOUND
        assert result.memory_ids == []


class TestGetEntityTool:
    async def test_finds_by_name(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "fact",
                    "entities": [{"name": "Nimbus Platform", "entity_type": "project"}],
                }
            ),
        )
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_entity(
                GetEntityArgs(name_or_id="Nimbus Platform"), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.OK
        assert result.data["name"] == "Nimbus Platform"

    async def test_unknown_entity_is_not_found(
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
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_entity(
                GetEntityArgs(name_or_id="Nonexistent Entity Zzz"), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.NOT_FOUND

    async def test_resolves_through_merge_redirect(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """The tool never duplicates redirect logic -- it inherits
        `RetrievalService.get_entity`/`find_entity_by_name`'s existing
        resolution through `EntityRepository.resolve` for free.
        """
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        old = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {"content": "old", "entities": [{"name": "OpenAI", "entity_type": "company"}]}
            ),
        )
        canonical = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "canonical",
                    "entities": [{"name": "Open AI Inc", "entity_type": "company"}],
                }
            ),
        )
        old_id = old.entities[0].id
        canonical_id = canonical.entities[0].id
        await EntityRepository(graph).merge(
            user_id=user_id,
            entity_id_a=canonical_id,
            entity_id_b=old_id,
            merge_id="m1",
            new_aliases=[],
            excluded_statuses=[],
            now=JAN,
            id_fn=lambda: str(uuid.uuid4()),
        )

        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_entity(
                GetEntityArgs(name_or_id="OpenAI"), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.OK
        assert result.data["entity_id"] == canonical_id


class TestTraverseGraphTool:
    async def test_multi_hop_traversal_reports_real_paths(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Nimbus Platform is built by Skyward Team",
                    "entities": [
                        {"name": "Nimbus Platform", "entity_type": "project"},
                        {"name": "Skyward Team", "entity_type": "concept"},
                    ],
                    "relationships": [
                        {
                            "source": "Nimbus Platform",
                            "predicate": "built_by",
                            "target": "Skyward Team",
                        }
                    ],
                }
            ),
        )
        root_id = next(e.id for e in created.entities if e.name == "Nimbus Platform")

        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await traverse_graph(
                TraverseGraphArgs(entity_id=root_id, max_hops=2), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.OK
        names = {n["name"] for n in result.data["nodes"]}
        assert "Skyward Team" in names
        skyward = next(n for n in result.data["nodes"] if n["name"] == "Skyward Team")
        assert skyward["hops"] == 1
        assert skyward["path"][-1]["predicate"] == "built_by"

    async def test_unknown_entity_id_is_not_found(
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
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await traverse_graph(
                TraverseGraphArgs(entity_id=str(uuid.uuid4())), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.NOT_FOUND


class TestGetEntityStateTool:
    async def test_current_vs_historical_split(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        old_memory = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        may = datetime(2026, 5, 1, tzinfo=UTC)
        new_memory = MemoryService(MemoryRepository(graph), now_fn=lambda: may)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        old = await old_memory.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses EasyOCR",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "EasyOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "EasyOCR"}
                    ],
                }
            ),
        )
        await new_memory.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses PaddleOCR",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "PaddleOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "PaddleOCR"}
                    ],
                    "supersedes": [old.memory.id],
                }
            ),
        )
        root_id = next(e.id for e in old.entities if e.name == "Drawing Extractor")

        async with db.session() as session:
            deps = _deps(session, graph, settings, now_fn=lambda: may)
            result = await get_entity_state(
                GetEntityStateArgs(entity_id=root_id), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.OK
        assert {r["target_entity_name"] for r in result.data["current"]} == {"PaddleOCR"}
        assert {r["target_entity_name"] for r in result.data["historical"]} == {"EasyOCR"}


class TestGetMemoryHistoryTool:
    async def test_walks_the_supersession_chain(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        old_memory = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        may = datetime(2026, 5, 1, tzinfo=UTC)
        new_memory = MemoryService(MemoryRepository(graph), now_fn=lambda: may)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        old = await old_memory.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "uses EasyOCR",
                    "entities": [{"name": "EasyOCR", "entity_type": "technology"}],
                }
            ),
        )
        new = await new_memory.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "uses PaddleOCR",
                    "entities": [{"name": "PaddleOCR", "entity_type": "technology"}],
                    "supersedes": [old.memory.id],
                }
            ),
        )
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_memory_history(
                GetMemoryHistoryArgs(memory_id=new.memory.id), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.OK
        assert set(result.memory_ids) == {old.memory.id, new.memory.id}

    async def test_unknown_memory_is_not_found(
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
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_memory_history(
                GetMemoryHistoryArgs(memory_id=str(uuid.uuid4())), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.NOT_FOUND


class TestGetSourceTool:
    async def test_returns_provenance_for_a_memory(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "fact",
                    "entities": [{"name": "X", "entity_type": "concept"}],
                    "source": {"source_type": "user_statement"},
                }
            ),
        )
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_source(
                GetSourceArgs(memory_id=created.memory.id), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.OK
        assert result.data["sources"][0]["source_type"] == "user_statement"

    async def test_unknown_memory_is_not_found(
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
        async with db.session() as session:
            deps = _deps(session, graph, settings)
            result = await get_source(
                GetSourceArgs(memory_id=str(uuid.uuid4())), user_id=user_id, deps=deps
            )
        assert result.status == ToolStatus.NOT_FOUND


class TestToolCrossUserIsolation:
    async def test_a_tool_call_naming_another_users_entity_finds_nothing(
        self,
        db,
        graph,
        settings,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
        theirs = await memory_service.create(
            user_id=other_user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "their secret",
                    "entities": [{"name": "Their Entity", "entity_type": "concept"}],
                }
            ),
        )
        their_entity_id = theirs.entities[0].id

        async with db.session() as session:
            deps = _deps(session, graph, settings)
            # Simulating a confused/adversarial model naming another user's
            # real entity id: dispatch is called with OUR user_id (the
            # authenticated caller), never the model's own opinion.
            entity_result = await get_entity(
                GetEntityArgs(name_or_id=their_entity_id), user_id=user_id, deps=deps
            )
            memory_result = await get_memory_history(
                GetMemoryHistoryArgs(memory_id=theirs.memory.id), user_id=user_id, deps=deps
            )
            source_result = await get_source(
                GetSourceArgs(memory_id=theirs.memory.id), user_id=user_id, deps=deps
            )
        assert entity_result.status == ToolStatus.NOT_FOUND
        assert memory_result.status == ToolStatus.NOT_FOUND
        assert source_result.status == ToolStatus.NOT_FOUND
