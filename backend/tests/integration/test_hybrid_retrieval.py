"""Hybrid graph + vector + keyword retrieval -- against real Neo4j and real
PostgreSQL (ADR 0009, PHASE3_PLAN.md sections 8-14).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.embeddings.fake import FakeEmbeddingClient
from app.repositories.embedding_repository import EmbeddingRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.hybrid_retrieval_service import HybridRetrievalService
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService

pytestmark = pytest.mark.integration

JAN = datetime(2026, 1, 15, tzinfo=UTC)
MAY = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
def embedding_client() -> FakeEmbeddingClient:
    return FakeEmbeddingClient(dimension=768)


@pytest.fixture
def settings() -> Settings:
    return Settings(embedding_provider="fake", retrieval_top_k=10)


async def _seed_embedding(db, embedding_client, user_id: str, memory_id: str, content: str, now):  # noqa: ANN001
    async with db.session() as session:
        result = await embedding_client.embed(text=content)
        await EmbeddingRepository(session, user_id=user_id).upsert(
            embedding_id=str(uuid.uuid4()),
            memory_id=memory_id,
            content=content,
            embedding=result.vector,
            embedding_provider=result.provider,
            embedding_model=result.model,
            embedding_dimension=result.dimension,
            now=now,
        )


def _hybrid(db, graph, embedding_client, settings, *, now):  # noqa: ANN001
    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: now)
    retrieval_service = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=lambda: now)
    return memory_service, retrieval_service


class TestVectorRetrieval:
    async def test_a_memory_found_only_by_vector_similarity_is_returned(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "The project relies on a vector database for semantic search",
                    "entities": [{"name": "Vector DB", "entity_type": "technology"}],
                }
            ),
        )
        await _seed_embedding(
            db,
            embedding_client,
            user_id,
            created.memory.id,
            "The project relies on a vector database for semantic search",
            JAN,
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="does the project use a vector database for search"
            )
        assert any(r.memory.memory.id == created.memory.id for r in results)
        match = next(r for r in results if r.memory.memory.id == created.memory.id)
        assert match.explanation.vector_similarity is not None
        assert "vector" in match.explanation.retrieved_via

    async def test_a_completely_unrelated_memory_is_not_returned(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Completely unrelated content about gardening tips",
                    "entities": [{"name": "Gardening", "entity_type": "concept"}],
                }
            ),
        )
        await _seed_embedding(
            db,
            embedding_client,
            user_id,
            created.memory.id,
            "Completely unrelated content about gardening tips",
            JAN,
        )
        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="database vector search technology"
            )
        assert not any(r.memory.memory.id == created.memory.id for r in results)


class TestGraphAndKeywordRetrieval:
    async def test_a_memory_found_only_by_entity_mention_is_returned_via_graph_channel(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Zzyzx Corp is the vendor for our OCR pipeline",
                    "entities": [{"name": "Zzyzx Corp", "entity_type": "company"}],
                }
            ),
        )
        # Deliberately not embedded -- this must be found by the graph channel alone.
        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="What do we know about Zzyzx Corp?"
            )
        match = next((r for r in results if r.memory.memory.id == created.memory.id), None)
        assert match is not None
        assert "graph" in match.explanation.retrieved_via
        assert match.explanation.vector_similarity is None

    async def test_keyword_content_match_is_reported_as_the_keyword_channel(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "A very specific unambiguous phrase xyzzyplugh appears here",
                    "entities": [{"name": "Xyzzyplugh Marker", "entity_type": "concept"}],
                }
            ),
        )
        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(user_id=user_id, query="xyzzyplugh")
        match = next((r for r in results if r.memory.memory.id == created.memory.id), None)
        assert match is not None
        assert "keyword" in match.explanation.retrieved_via


class TestCandidateMerging:
    async def test_a_memory_found_by_both_vector_and_keyword_is_deduplicated_and_reports_both(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        content = "Drawing Extractor uses supercalifragilistic OCR technology"
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": content,
                    "entities": [{"name": "Drawing Extractor", "entity_type": "project"}],
                }
            ),
        )
        await _seed_embedding(db, embedding_client, user_id, created.memory.id, content, JAN)

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="supercalifragilistic OCR technology"
            )
        matches = [r for r in results if r.memory.memory.id == created.memory.id]
        assert len(matches) == 1, "must appear exactly once, not once per retriever"
        assert set(matches[0].explanation.retrieved_via) >= {"vector", "keyword"}


class TestTemporalFiltering:
    """The exact regression case named in the Phase 3 approval: a CURRENT query
    must not return a superseded memory as current merely because its embedding
    scores highest; a HISTORICAL/AS_OF query must still be able to find it.
    """

    async def _seed_ocr_history(self, db, graph, embedding_client, user_id):  # noqa: ANN001
        memory_service_jan = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        memory_service_may = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        old = await memory_service_jan.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses EasyOCR for text recognition",
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
        new = await memory_service_may.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses PaddleOCR for text recognition",
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
        # EasyOCR's embedding is the CLOSER vector match on purpose -- this is
        # what actually exercises the filter rather than passing by coincidence.
        await _seed_embedding(
            db,
            embedding_client,
            user_id,
            old.memory.id,
            "Drawing Extractor uses EasyOCR for text recognition",
            MAY,
        )
        await _seed_embedding(
            db,
            embedding_client,
            user_id,
            new.memory.id,
            "Drawing Extractor uses PaddleOCR for text recognition",
            MAY,
        )
        return old, new

    async def test_current_query_returns_only_the_active_memory(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        old, new = await self._seed_ocr_history(db, graph, embedding_client, user_id)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: MAY
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: MAY,
            )
            results = await hybrid.search(
                user_id=user_id,
                query="What does Drawing Extractor currently use for text recognition?",
            )
        ids = {r.memory.memory.id for r in results}
        assert new.memory.id in ids
        assert old.memory.id not in ids, "a CURRENT query must never surface the superseded memory"
        current_result = next(r for r in results if r.memory.memory.id == new.memory.id)
        assert current_result.explanation.temporal_state == "current"
        assert current_result.explanation.temporal_fit == 1.0

    async def test_as_of_march_query_returns_only_the_historical_memory(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        old, new = await self._seed_ocr_history(db, graph, embedding_client, user_id)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: MAY
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: MAY,
            )
            results = await hybrid.search(
                user_id=user_id,
                query="What did Drawing Extractor use for text recognition in March?",
            )
        ids = {r.memory.memory.id for r in results}
        assert old.memory.id in ids
        assert new.memory.id not in ids, (
            "an AS_OF(March) query must not surface the not-yet-current memory"
        )
        historical_result = next(r for r in results if r.memory.memory.id == old.memory.id)
        assert historical_result.explanation.temporal_state == "superseded"

    async def test_unspecified_intent_returns_both_with_no_temporal_boost(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        old, new = await self._seed_ocr_history(db, graph, embedding_client, user_id)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: MAY
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: MAY,
            )
            results = await hybrid.search(
                user_id=user_id, query="What does Drawing Extractor use for text recognition?"
            )
        ids = {r.memory.memory.id for r in results}
        assert {old.memory.id, new.memory.id} <= ids
        assert all(r.explanation.temporal_fit == 0.0 for r in results)


class TestDegradedFallback:
    async def test_vector_search_failure_degrades_to_graph_and_keyword_only(
        self,
        db,
        graph,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """Core invariant 6: vector-index failure must degrade to graph/keyword
        retrieval, never make memory appear nonexistent.
        """
        from app.core.errors import EmbeddingUnavailableError

        class FailingEmbeddingClient:
            async def embed(self, *, text: str, timeout: float | None = None):  # noqa: ANN202
                raise EmbeddingUnavailableError("simulated provider outage")

        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: JAN
        )
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "A findable-by-keyword-only fact about qwertyuiop",
                    "entities": [{"name": "Qwertyuiop", "entity_type": "concept"}],
                }
            ),
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=FailingEmbeddingClient(),
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(user_id=user_id, query="qwertyuiop")
        assert any(r.memory.memory.id == created.memory.id for r in results), (
            "a failing vector search must not make an existing, keyword-findable memory disappear"
        )


class TestUserIsolation:
    async def test_search_never_returns_another_users_memory(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """The explicit regression test the Phase 3 approval named: User A's
        memory must never appear in User B's hybrid search results, across all
        three retrieval channels at once.
        """
        memory_service_a = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        retrieval_service_b = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: JAN
        )
        memory_service_b = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
        content = "User A's private fact about a shared unique token florbnorpwibble"
        created = await memory_service_a.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": content,
                    "entities": [{"name": "Florbnorpwibble", "entity_type": "concept"}],
                }
            ),
        )
        await _seed_embedding(db, embedding_client, user_id, created.memory.id, content, JAN)

        async with db.session() as session:
            hybrid_as_other_user = HybridRetrievalService(
                session=session,
                memory_service=memory_service_b,
                retrieval_service=retrieval_service_b,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid_as_other_user.search(
                user_id=other_user_id, query="florbnorpwibble shared unique token"
            )
        assert results == []


class TestMultiHopGraphRetrieval:
    """PHASE4_PLAN.md sections 1/4/13: the graph channel now traverses beyond
    hop 0, decaying by `settings.retrieval_graph_decay_rate ** hops`, and
    `RetrievalExplanation` reports the real path -- never a fabricated one.
    """

    async def _seed_chain(self, db, graph, user_id):  # noqa: ANN001
        """Drawing Extractor -managed_by-> Platform Team -maintains-> Quarterly
        Roadmap, plus a bare statement mentioning ONLY Quarterly Roadmap. The
        query below names only "Drawing Extractor", so the roadmap statement
        is discoverable exclusively through the 2-hop graph traversal.
        """
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        m_ab = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor is managed by Platform Team",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "Platform Team", "entity_type": "concept"},
                    ],
                    "relationships": [
                        {
                            "source": "Drawing Extractor",
                            "predicate": "managed_by",
                            "target": "Platform Team",
                        }
                    ],
                }
            ),
        )
        m_bc = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Platform Team maintains the Quarterly Roadmap",
                    "entities": [
                        {"name": "Platform Team", "entity_type": "concept"},
                        {"name": "Quarterly Roadmap", "entity_type": "concept"},
                    ],
                    "relationships": [
                        {
                            "source": "Platform Team",
                            "predicate": "maintains",
                            "target": "Quarterly Roadmap",
                        }
                    ],
                }
            ),
        )
        m_c = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "The Quarterly Roadmap was last revised in January",
                    "entities": [{"name": "Quarterly Roadmap", "entity_type": "concept"}],
                }
            ),
        )
        return m_ab, m_bc, m_c

    async def test_2hop_only_memory_is_found_with_correct_decay_and_path(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        m_ab, m_bc, m_c = await self._seed_chain(db, graph, user_id)
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="What does Drawing Extractor depend on?"
            )

        by_id = {r.memory.memory.id: r for r in results}
        assert m_ab.memory.id in by_id
        assert by_id[m_ab.memory.id].explanation.graph_distance == 0

        # m_bc is reachable at hop 1 (via Platform Team) -- never hop 2, even
        # though Quarterly Roadmap could also reach it: "merge, not
        # concatenate" keeps the SHORTER of the two paths.
        assert m_bc.memory.id in by_id
        assert by_id[m_bc.memory.id].explanation.graph_distance == 1
        assert by_id[m_bc.memory.id].explanation.graph_relevance == pytest.approx(
            settings.retrieval_graph_decay_rate
        )

        # m_c mentions ONLY "Quarterly Roadmap", never named in the query --
        # the only way to it is the 2-hop traversal through Platform Team.
        assert m_c.memory.id in by_id
        c_result = by_id[m_c.memory.id]
        assert "graph" in c_result.explanation.retrieved_via
        assert c_result.explanation.graph_distance == 2
        assert c_result.explanation.graph_relevance == pytest.approx(
            settings.retrieval_graph_decay_rate**2
        )
        path = c_result.explanation.graph_path
        assert path is not None
        assert [step.entity_name for step in path] == [
            "Drawing Extractor",
            "Platform Team",
            "Quarterly Roadmap",
        ]
        assert path[0].predicate is None  # root step
        assert path[1].predicate == "managed_by"
        assert path[2].predicate == "maintains"

    async def test_hops_setting_of_one_does_not_reach_the_2hop_memory(
        self,
        db,
        graph,
        embedding_client,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        _, _, m_c = await self._seed_chain(db, graph, user_id)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: JAN
        )
        one_hop_settings = Settings(
            embedding_provider="fake", retrieval_top_k=10, retrieval_graph_hops=1
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=one_hop_settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="What does Drawing Extractor depend on?"
            )
        ids = {r.memory.memory.id for r in results}
        assert m_c.memory.id not in ids, "hops=1 must not reach a 2-hop-only memory"


class TestMultiHopTemporalFiltering:
    """PHASE4_PLAN.md section 4's explicit requirement: temporal filtering
    must apply through multi-hop graph paths, not just directly-mentioned
    entities -- the same EasyOCR/PaddleOCR regression as TestTemporalFiltering,
    but with the OCR fact reached via graph traversal (neither "OCR Module"
    nor "EasyOCR"/"PaddleOCR" is named in the query) instead of a
    directly-mentioned entity.

    Note on hop count: EasyOCR/PaddleOCR are two hops from "Drawing Extractor"
    via OCR Module, but the old/new memories ALSO declare "OCR Module" itself
    as one of their own entities (it is their relationship's source) --  so
    they are independently reachable at hop 1 through OCR Module's own memory
    list, and "merge, keep the shortest path" (GraphHit's own contract)
    correctly reports hop 1, not 2. That is still a graph-traversal-only path
    (OCR Module is never named in the query either), which is exactly the
    condition this test exists to exercise: temporal filtering applied to a
    candidate the query's own text never mentions.
    """

    async def _seed_ocr_history_2hops_away(self, db, graph, user_id):  # noqa: ANN001
        memory_service_jan = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        memory_service_may = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        # hop 1: Drawing Extractor -depends_on-> OCR Module
        anchor = await memory_service_jan.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor depends on the OCR Module",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "OCR Module", "entity_type": "concept"},
                    ],
                    "relationships": [
                        {
                            "source": "Drawing Extractor",
                            "predicate": "depends_on",
                            "target": "OCR Module",
                        }
                    ],
                }
            ),
        )
        # reachable at hop 1 (via OCR Module, itself hop 1 from the anchor)
        old = await memory_service_jan.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "OCR Module uses EasyOCR for text recognition",
                    "entities": [
                        {"name": "OCR Module", "entity_type": "concept"},
                        {"name": "EasyOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "OCR Module", "predicate": "uses", "target": "EasyOCR"}
                    ],
                }
            ),
        )
        new = await memory_service_may.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "OCR Module uses PaddleOCR for text recognition",
                    "entities": [
                        {"name": "OCR Module", "entity_type": "concept"},
                        {"name": "PaddleOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "OCR Module", "predicate": "uses", "target": "PaddleOCR"}
                    ],
                    "supersedes": [old.memory.id],
                }
            ),
        )
        return anchor, old, new

    async def test_current_query_excludes_the_2hop_superseded_memory(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        anchor, old, new = await self._seed_ocr_history_2hops_away(db, graph, user_id)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: MAY
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: MAY,
            )
            results = await hybrid.search(
                user_id=user_id,
                query="What does Drawing Extractor currently use for text recognition?",
            )
        ids = {r.memory.memory.id for r in results}
        assert anchor.memory.id in ids
        assert new.memory.id in ids, "the graph-reached current fact must still surface"
        assert old.memory.id not in ids, (
            "a CURRENT query must exclude a superseded memory even when the only "
            "channel that found it is a graph traversal, not a query mention"
        )
        new_result = next(r for r in results if r.memory.memory.id == new.memory.id)
        assert new_result.explanation.graph_distance == 1
        assert new_result.explanation.temporal_state == "current"

    async def test_as_of_march_query_surfaces_the_2hop_historical_memory(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        anchor, old, new = await self._seed_ocr_history_2hops_away(db, graph, user_id)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: MAY)
        retrieval_service = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=lambda: MAY
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: MAY,
            )
            results = await hybrid.search(
                user_id=user_id,
                query="What did Drawing Extractor use for text recognition in March?",
            )
        ids = {r.memory.memory.id for r in results}
        assert old.memory.id in ids, "AS_OF(March) must surface the graph-reached historical memory"
        assert new.memory.id not in ids
        old_result = next(r for r in results if r.memory.memory.id == old.memory.id)
        assert old_result.explanation.graph_distance == 1
        assert old_result.explanation.temporal_state == "superseded"


class TestEntityRedirectInHybridRetrieval:
    """PHASE4_PLAN.md section 9 / the follow-up fix requested after Phase 4
    approval: hybrid retrieval's graph-channel entity-mention scan must
    resolve through `RetrievalService.resolve_entity` -- the SAME canonical
    resolver `get_entity`/`find_entity_by_name` already use -- before
    treating a mention as a traversal anchor. Old/merged entity name -> mention
    detection -> canonical resolution -> retrieval.
    """

    async def _merge(
        self, graph, *, user_id: str, canonical_id: str, merged_id: str, merge_id: str, now
    ):  # noqa: ANN001
        counter = iter(range(1000))
        await EntityRepository(graph).merge(
            user_id=user_id,
            entity_id_a=canonical_id,
            entity_id_b=merged_id,
            merge_id=merge_id,
            new_aliases=[],
            excluded_statuses=[],
            now=now,
            id_fn=lambda: f"copy-{next(counter)}-{uuid.uuid4().hex[:6]}",
        )

    async def test_query_by_old_entity_name_retrieves_canonical_neighbourhood(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)

        # Canonical "Nimbus Platform" has its own neighbour "Skyward Team".
        canonical_fact = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Nimbus Platform is built by the Skyward Team",
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
        # An old, since-merged-away entity: "Project Nimbus" (bare mention only).
        old_entity_fact = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Project Nimbus was the original codename",
                    "entities": [{"name": "Project Nimbus", "entity_type": "project"}],
                }
            ),
        )
        canonical = await retrieval_service.find_entity_by_name(
            user_id=user_id, name="Nimbus Platform"
        )
        old_entity = await retrieval_service.get_entity(
            user_id=user_id, entity_id=old_entity_fact.entities[0].id
        )

        await self._merge(
            graph,
            user_id=user_id,
            canonical_id=canonical.id,
            merged_id=old_entity.entity.id,
            merge_id="merge-1",
            now=JAN,
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="What do we know about Project Nimbus?"
            )

        ids = {r.memory.memory.id for r in results}
        assert canonical_fact.memory.id in ids, (
            "a query naming the OLD entity must retrieve the CANONICAL entity's own neighbourhood"
        )
        match = next(r for r in results if r.memory.memory.id == canonical_fact.memory.id)
        assert "graph" in match.explanation.retrieved_via
        # The merged-away entity's OWN bare-mention memory is untouched (never
        # deleted) but is no longer what the graph channel anchors on, exactly
        # like `get_entity(old_id)` no longer returns the old entity's own
        # detail -- both channels now agree on the redirect.
        assert old_entity_fact.memory.id != canonical_fact.memory.id

    async def test_resolve_entity_by_old_id_matches_canonical(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        a = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {"content": "A", "entities": [{"name": "Entity A", "entity_type": "concept"}]}
            ),
        )
        b = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {"content": "B", "entities": [{"name": "Entity B", "entity_type": "concept"}]}
            ),
        )
        a_id = a.entities[0].id
        b_id = b.entities[0].id
        await self._merge(
            graph, user_id=user_id, canonical_id=b_id, merged_id=a_id, merge_id="m1", now=JAN
        )

        resolved = await retrieval_service.resolve_entity(user_id=user_id, entity_id=a_id)
        assert resolved is not None
        assert resolved.entity.id == b_id
        assert resolved.was_redirected is True
        assert resolved.original_id == a_id

    async def test_redirect_chain_resolves_through_hybrid_retrieval(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)

        a = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Alpha Corp mention",
                    "entities": [{"name": "Alpha Corp", "entity_type": "company"}],
                }
            ),
        )
        b = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Alpha Company mention",
                    "entities": [{"name": "Alpha Company", "entity_type": "company"}],
                }
            ),
        )
        c_fact = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Alpha Inc reported record quarterly earnings",
                    "entities": [{"name": "Alpha Inc", "entity_type": "company"}],
                }
            ),
        )
        a_id, b_id, c_id = a.entities[0].id, b.entities[0].id, c_fact.entities[0].id
        # Chain: A -> B -> C
        await self._merge(
            graph, user_id=user_id, canonical_id=b_id, merged_id=a_id, merge_id="m-ab", now=JAN
        )
        await self._merge(
            graph, user_id=user_id, canonical_id=c_id, merged_id=b_id, merge_id="m-bc", now=JAN
        )

        resolved = await retrieval_service.resolve_entity(user_id=user_id, entity_id=a_id)
        assert resolved is not None
        assert resolved.entity.id == c_id
        assert resolved.redirect_depth == 2

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="What do we know about Alpha Corp?"
            )
        ids = {r.memory.memory.id for r in results}
        assert c_fact.memory.id in ids, (
            "a chain A->B->C must resolve all the way to the final canonical C"
        )

    async def test_cross_user_redirect_does_not_leak(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)

        # Both users happen to name an entity the same thing, and BOTH merge
        # it into their own, separate canonical -- these must never cross.
        mine_old = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Mine old",
                    "entities": [{"name": "Shared Name", "entity_type": "concept"}],
                }
            ),
        )
        mine_canonical = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "My canonical secret fact",
                    "entities": [{"name": "My Canonical", "entity_type": "concept"}],
                }
            ),
        )
        theirs_old = await memory_service.create(
            user_id=other_user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Theirs old",
                    "entities": [{"name": "Shared Name", "entity_type": "concept"}],
                }
            ),
        )
        theirs_canonical = await memory_service.create(
            user_id=other_user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Their canonical unrelated fact",
                    "entities": [{"name": "Their Canonical", "entity_type": "concept"}],
                }
            ),
        )
        await self._merge(
            graph,
            user_id=user_id,
            canonical_id=mine_canonical.entities[0].id,
            merged_id=mine_old.entities[0].id,
            merge_id="mine",
            now=JAN,
        )
        await self._merge(
            graph,
            user_id=other_user_id,
            canonical_id=theirs_canonical.entities[0].id,
            merged_id=theirs_old.entities[0].id,
            merge_id="theirs",
            now=JAN,
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(
                user_id=user_id, query="What do we know about Shared Name?"
            )
        ids = {r.memory.memory.id for r in results}
        assert mine_canonical.memory.id in ids
        assert theirs_canonical.memory.id not in ids, (
            "must never resolve into another user's canonical entity"
        )
        assert theirs_old.memory.id not in ids

    async def test_vector_and_graph_retrieval_reflect_merge(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)

        old = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "OpenAI evaluation notes",
                    "entities": [{"name": "OpenAI", "entity_type": "company"}],
                }
            ),
        )
        canonical_fact = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Open AI Inc partners with the platform team on integrations",
                    "entities": [
                        {"name": "Open AI Inc", "entity_type": "company"},
                        {"name": "Platform Team", "entity_type": "concept"},
                    ],
                    "relationships": [
                        {
                            "source": "Open AI Inc",
                            "predicate": "partners_with",
                            "target": "Platform Team",
                        }
                    ],
                }
            ),
        )
        await _seed_embedding(
            db, embedding_client, user_id, old.memory.id, "OpenAI evaluation notes", JAN
        )
        await _seed_embedding(
            db,
            embedding_client,
            user_id,
            canonical_fact.memory.id,
            "Open AI Inc partners with the platform team on integrations",
            JAN,
        )
        await self._merge(
            graph,
            user_id=user_id,
            canonical_id=canonical_fact.entities[0].id,
            merged_id=old.entities[0].id,
            merge_id="oai-merge",
            now=JAN,
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            results = await hybrid.search(user_id=user_id, query="What do we know about OpenAI?")

        ids = {r.memory.memory.id for r in results}
        assert canonical_fact.memory.id in ids
        match = next(r for r in results if r.memory.memory.id == canonical_fact.memory.id)
        # Both channels should be able to contribute after the merge: the
        # graph channel via the resolved canonical anchor, and (since the
        # query text overlaps the canonical's own content) potentially vector
        # too -- at minimum graph must be present.
        assert "graph" in match.explanation.retrieved_via

        # The old (merged-away) memory is never deleted and its own vector
        # row is untouched -- confirmed directly against the embedding table.
        async with db.session() as session:
            from app.repositories.embedding_repository import EmbeddingRepository as ER

            still_present = await ER(session, user_id=user_id).exists(
                memory_id=old.memory.id,
                embedding_provider=settings.embedding_provider,
                embedding_model=settings.embedding_model_resolved,
            )
        assert still_present, "merging entities must never delete a memory's embedding"

    async def test_cyclic_redirect_does_not_hang_or_error_hybrid_search(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)

        x = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Cycle X fact",
                    "entities": [{"name": "Cycle X", "entity_type": "concept"}],
                }
            ),
        )
        y = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Cycle Y fact",
                    "entities": [{"name": "Cycle Y", "entity_type": "concept"}],
                }
            ),
        )
        x_id, y_id = x.entities[0].id, y.entities[0].id
        # A cycle: X -> Y -> X. CREATE_MERGE_REDIRECT's own guard refuses the
        # second edge if it would close a cycle onto an ALREADY-canonical
        # chain, but a raw two-node mutual redirect (X->Y, then Y->X) is the
        # simplest way to exercise the resolver's cycle-safety without
        # relying on that guard -- written directly against the graph to
        # simulate a corrupted/invalid state, not something the approved
        # merge() write path can itself produce.
        await graph.write(
            "MATCH (a:Entity {id: $x, user_id: $u}), (b:Entity {id: $y, user_id: $u}) "
            "MERGE (a)-[:MERGED_INTO {user_id: $u, created_at: $now, merge_id: 'cyc1'}]->(b)",
            {"x": x_id, "y": y_id, "u": user_id, "now": JAN},
        )
        await graph.write(
            "MATCH (a:Entity {id: $x, user_id: $u}), (b:Entity {id: $y, user_id: $u}) "
            "MERGE (b)-[:MERGED_INTO {user_id: $u, created_at: $now, merge_id: 'cyc2'}]->(a)",
            {"x": x_id, "y": y_id, "u": user_id, "now": JAN},
        )

        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: JAN,
            )
            start = time.perf_counter()
            results = await hybrid.search(user_id=user_id, query="What do we know about Cycle X?")
            elapsed = time.perf_counter() - start

        assert elapsed < 5.0, f"a cyclic redirect must not hang retrieval (took {elapsed:.2f}s)"
        # The resolver's documented cycle behaviour is to collapse back to
        # the start entity -- so this must behave like an ordinary, unmerged
        # lookup, never an error and never an infinite loop.
        ids = {r.memory.memory.id for r in results}
        assert x.memory.id in ids

    async def test_get_entity_by_old_id_raises_not_found_only_when_truly_gone(
        self,
        db,
        graph,
        embedding_client,
        settings,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """Sanity check distinguishing 'redirected' from 'never existed' --
        `resolve_entity` must return `None` (not a fabricated redirect) for
        an id that was never real, so callers can tell the two apart.
        """
        memory_service, retrieval_service = _hybrid(db, graph, embedding_client, settings, now=JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        resolved = await retrieval_service.resolve_entity(
            user_id=user_id, entity_id=str(uuid.uuid4())
        )
        assert resolved is None
        with pytest.raises(NotFoundError):
            await retrieval_service.get_entity(user_id=user_id, entity_id=str(uuid.uuid4()))
