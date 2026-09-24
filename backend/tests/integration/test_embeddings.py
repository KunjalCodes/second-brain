"""Embedding storage, similarity search, isolation, and the backfill sweep --
against real Neo4j and real PostgreSQL (ADR 0009, PHASE3_PLAN.md).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import ConfigurationError
from app.db.engine import Database
from app.embeddings.fake import FakeEmbeddingClient, deterministic_vector
from app.repositories.embedding_repository import EmbeddingRepository, content_hash
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.semantic.embedding_pipeline import embed_and_store_memory, run_embedding_backfill_tick
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.integration

JAN = datetime(2026, 1, 15, tzinfo=UTC)


@pytest.fixture
def embedding_client() -> FakeEmbeddingClient:
    return FakeEmbeddingClient(dimension=768)


class TestContentHash:
    def test_same_text_hashes_identically(self) -> None:
        assert content_hash("hello world") == content_hash("hello world")

    def test_different_text_hashes_differently(self) -> None:
        assert content_hash("hello world") != content_hash("goodbye world")


class TestEmbeddingRepository:
    async def test_upsert_then_search_finds_the_nearest_neighbor(
        self,
        db,
        user_id: str,
        pg_cleanup: None,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            repo = EmbeddingRepository(session, user_id=user_id)
            close = deterministic_vector(
                "the project uses gemini for classification", dimension=768
            )
            far = deterministic_vector("completely unrelated text about weather", dimension=768)
            await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-close",
                content="c1",
                embedding=close,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )
            await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-far",
                content="c2",
                embedding=far,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )

            query = deterministic_vector(
                "the project uses gemini for classification of documents", dimension=768
            )
            results = await repo.search(
                query_embedding=query, embedding_provider="fake", embedding_model="v1", limit=2
            )
        assert [r.memory_id for r in results] == ["mem-close", "mem-far"]
        assert results[0].similarity > results[1].similarity

    async def test_upsert_is_idempotent_for_the_same_memory_provider_model(
        self,
        db,
        user_id: str,
        pg_cleanup: None,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            repo = EmbeddingRepository(session, user_id=user_id)
            v = deterministic_vector("hello", dimension=768)
            first = await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-1",
                content="hello",
                embedding=v,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )
            second = await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-1",
                content="hello",
                embedding=v,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )
        assert first is True
        assert second is False

    async def test_a_different_model_gets_its_own_row_not_an_overwrite(
        self,
        db,
        user_id: str,
        pg_cleanup: None,  # noqa: ANN001
    ) -> None:
        """ADR 0009: embeddings are versioned by (provider, model), never
        overwritten in place -- a model migration must be able to keep both.
        """
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            repo = EmbeddingRepository(session, user_id=user_id)
            v = deterministic_vector("hello", dimension=768)
            await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-1",
                content="hello",
                embedding=v,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )
            await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-1",
                content="hello",
                embedding=v,
                embedding_provider="fake",
                embedding_model="v2",
                embedding_dimension=768,
                now=JAN,
            )
            exists_v1 = await repo.exists(
                memory_id="mem-1", embedding_provider="fake", embedding_model="v1"
            )
            exists_v2 = await repo.exists(
                memory_id="mem-1", embedding_provider="fake", embedding_model="v2"
            )
        assert exists_v1 is True
        assert exists_v2 is True

    async def test_find_similar_to_memory_excludes_itself(
        self,
        db,
        user_id: str,
        pg_cleanup: None,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            repo = EmbeddingRepository(session, user_id=user_id)
            v = deterministic_vector("hello world", dimension=768)
            await repo.upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-a",
                content="hello world",
                embedding=v,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )
            similar = await repo.find_similar_to_memory(
                memory_id="mem-a", embedding_provider="fake", embedding_model="v1", limit=5
            )
        assert similar == []

    async def test_search_never_returns_another_users_embedding(
        self,
        db,
        user_id: str,
        other_user_id: str,
        pg_cleanup: None,  # noqa: ANN001
    ) -> None:
        """The explicit isolation regression test the Phase 3 approval named:
        User A's memory must never appear in User B's vector search.
        """
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
            v = deterministic_vector("a shared, identical statement", dimension=768)
            await EmbeddingRepository(session, user_id=user_id).upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id="mem-a",
                content="x",
                embedding=v,
                embedding_provider="fake",
                embedding_model="v1",
                embedding_dimension=768,
                now=JAN,
            )
            results_as_other_user = await EmbeddingRepository(
                session, user_id=other_user_id
            ).search(query_embedding=v, embedding_provider="fake", embedding_model="v1", limit=10)
        assert results_as_other_user == []


class TestEmbedAndStoreMemory:
    async def test_stores_a_retrievable_embedding(
        self,
        db,
        user_id: str,
        pg_cleanup: None,
        embedding_client,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        from app.core.config import get_settings

        stored = await embed_and_store_memory(
            user_id=user_id,
            memory_id="mem-1",
            content="hello world",
            db=db,
            embedding_client=embedding_client,
            settings=get_settings(),
            now_fn=lambda: JAN,
        )
        assert stored is True
        async with db.session() as session:
            exists = await EmbeddingRepository(session, user_id=user_id).exists(
                memory_id="mem-1",
                embedding_provider="fake",
                embedding_model=embedding_client._model,
            )
        assert exists is True

    async def test_embedding_failure_never_raises(
        self,
        db,
        user_id: str,
        pg_cleanup: None,  # noqa: ANN001
    ) -> None:
        """Core invariant: embedding provider failure must never prevent (or
        appear to break) memory creation.
        """
        from app.core.config import get_settings
        from app.core.errors import EmbeddingUnavailableError

        class FailingClient:
            async def embed(self, *, text: str, timeout: float | None = None):  # noqa: ANN202
                raise EmbeddingUnavailableError("simulated provider outage")

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)

        stored = await embed_and_store_memory(
            user_id=user_id,
            memory_id="mem-1",
            content="hello world",
            db=db,
            embedding_client=FailingClient(),
            settings=get_settings(),
            now_fn=lambda: JAN,
        )
        assert stored is False


class TestEmbeddingDimensionValidation:
    async def test_database_connect_rejects_a_mismatched_dimension(self) -> None:
        """PHASE3_PLAN.md section 12: the configured dimension must match the
        pgvector column's declared width, checked at startup.
        """
        from app.core.config import Settings, get_settings

        real_settings = get_settings()
        bad_settings = Settings(database_url=real_settings.database_url, embedding_dimension=1536)
        bad_db = Database(bad_settings)
        with pytest.raises(ConfigurationError, match="EMBEDDING_DIMENSION"):
            await bad_db.connect()


class TestBackfill:
    async def test_backfill_embeds_a_memory_that_predates_phase_3(
        self,
        db,
        graph,
        user_id: str,
        pg_cleanup: None,
        cleanup,
        embedding_client,  # noqa: ANN001
    ) -> None:
        """Simulates a Phase 1/2 memory that was never embedded (created before
        this feature existed) -- the backfill sweep must find and embed it.
        """
        from app.core.config import get_settings
        from app.repositories.memory_repository import MemoryRepository

        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Pre-existing memory with no embedding yet",
                    "entities": [{"name": "Legacy Thing", "entity_type": "concept"}],
                }
            ),
        )

        counts = await run_embedding_backfill_tick(
            db=db,
            graph_client=graph,
            embedding_client=embedding_client,
            settings=get_settings(),
            user_id=user_id,
            now_fn=lambda: JAN,
        )
        assert counts["embedded"] == 1

        async with db.session() as session:
            exists = await EmbeddingRepository(session, user_id=user_id).exists(
                memory_id=created.memory.id,
                embedding_provider="fake",
                embedding_model=embedding_client._model,
            )
        assert exists is True

    async def test_backfill_is_idempotent(
        self,
        db,
        graph,
        user_id: str,
        pg_cleanup: None,
        cleanup,
        embedding_client,  # noqa: ANN001
    ) -> None:
        from app.core.config import get_settings
        from app.repositories.memory_repository import MemoryRepository

        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "One memory, embedded exactly once",
                    "entities": [{"name": "Idempotent Thing", "entity_type": "concept"}],
                }
            ),
        )
        settings = get_settings()
        first = await run_embedding_backfill_tick(
            db=db,
            graph_client=graph,
            embedding_client=embedding_client,
            settings=settings,
            user_id=user_id,
            now_fn=lambda: JAN,
        )
        second = await run_embedding_backfill_tick(
            db=db,
            graph_client=graph,
            embedding_client=embedding_client,
            settings=settings,
            user_id=user_id,
            now_fn=lambda: JAN,
        )
        assert first["embedded"] == 1
        assert second["embedded"] == 0
        assert second["skipped_already_present"] == 1

        async with db.session() as session:
            from sqlalchemy import text

            count = (
                await session.execute(
                    text("SELECT count(*) FROM memory_embeddings WHERE user_id = :u"),
                    {"u": user_id},
                )
            ).scalar_one()
        assert count == 1, "running the backfill twice must not create duplicate rows"

    async def test_backfill_is_user_scoped(
        self,
        db,
        graph,
        user_id: str,
        other_user_id: str,
        pg_cleanup: None,
        cleanup,
        embedding_client,  # noqa: ANN001
    ) -> None:
        from app.core.config import get_settings
        from app.repositories.memory_repository import MemoryRepository

        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
        await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "User A's memory",
                    "entities": [{"name": "A Thing", "entity_type": "concept"}],
                }
            ),
        )
        counts = await run_embedding_backfill_tick(
            db=db,
            graph_client=graph,
            embedding_client=embedding_client,
            settings=get_settings(),
            user_id=other_user_id,
            now_fn=lambda: JAN,
        )
        assert counts["scanned"] == 0
        assert counts["embedded"] == 0
