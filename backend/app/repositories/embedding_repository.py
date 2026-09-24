"""PostgreSQL persistence for memory embeddings (ADR 0009, PHASE3_PLAN.md section 2).

The only file that writes or queries `memory_embeddings`. Every method is
user-scoped through `UserScopedRepository`, exactly like every other Phase 2/3
PostgreSQL repository -- covered by the existing `test_sql_safety.py` AST audit
with no changes needed to that audit.

This repository never touches Neo4j and never decides whether a memory is
current, valid, or should be returned. It answers exactly one kind of question:
"which vectors are closest to this one, for this user, under this embedding
model" -- everything else (temporal correctness, status, ranking) is the caller's
job (`app/services/hybrid_retrieval_service.py`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert

from app.db.models import MemoryEmbedding as EmbeddingRow
from app.repositories.sql_base import UserScopedRepository


def content_hash(content: str) -> str:
    """Deterministic hash of the exact text a memory was embedded from.

    Not a drift detector -- memory content is immutable (`MemoryUpdate` excludes
    it) -- but a defense-in-depth guard: if a re-embed attempt's content hash ever
    disagrees with what is already stored for a `memory_id`, something upstream
    reused or corrupted an id, and that should be flagged rather than silently
    overwritten.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SimilarMemory:
    """One nearest-neighbor result: a memory id and its similarity to the query."""

    memory_id: str
    similarity: float  # cosine similarity, [0, 1] roughly -- see `_similarity`


def _similarity(cosine_distance: float) -> float:
    """pgvector's `<=>` operator is cosine *distance* (`1 - cosine_similarity`)."""
    return 1.0 - cosine_distance


class EmbeddingRepository(UserScopedRepository):
    """CRUD and similarity search over `memory_embeddings`."""

    async def upsert(
        self,
        *,
        embedding_id: str,
        memory_id: str,
        content: str,
        embedding: list[float],
        embedding_provider: str,
        embedding_model: str,
        embedding_dimension: int,
        now: datetime,
    ) -> bool:
        """Insert one embedding row, or do nothing if this `(memory_id, provider,
        model)` already has one (idempotency layer, mirrors
        `ExtractionCandidateRepository.record_candidate`'s `ON CONFLICT DO NOTHING`
        shape).

        Returns `True` if a new row was actually inserted, `False` if one already
        existed -- used by the embedding pipeline and backfill sweep to report
        accurate `embedded`/`skipped_already_present` counts.
        """
        stmt = (
            insert(EmbeddingRow)
            .values(
                id=embedding_id,
                user_id=self._user_id,
                memory_id=memory_id,
                content_hash=content_hash(content),
                embedding=embedding,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["memory_id", "embedding_provider", "embedding_model"]
            )
            .returning(EmbeddingRow.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def exists(
        self, *, memory_id: str, embedding_provider: str, embedding_model: str
    ) -> bool:
        """Whether this memory already has a row for the given `(provider, model)`.

        Used by the backfill sweep to find memories that still need embedding --
        the anti-join happens in the backfill driver (PHASE3_PLAN.md section 13:
        "the source of truth for which memories exist is Neo4j"), one page of
        Neo4j memory ids at a time, checked here.
        """
        stmt = self._select_columns(EmbeddingRow, EmbeddingRow.id).where(
            EmbeddingRow.memory_id == memory_id,
            EmbeddingRow.embedding_provider == embedding_provider,
            EmbeddingRow.embedding_model == embedding_model,
        )
        result = await self._session.execute(stmt)
        return result.first() is not None

    async def search(
        self,
        *,
        query_embedding: list[float],
        embedding_provider: str,
        embedding_model: str,
        limit: int,
    ) -> list[SimilarMemory]:
        """Nearest neighbors by cosine distance, scoped to this user and this
        embedding model.

        The `WHERE user_id = ...` clause is not optional or an afterthought -- it
        is injected by `_select` exactly like every other query in this
        repository package, which is what makes a cross-user vector-search leak
        structurally impossible to express here, the same guarantee Cypher's
        `$user_id` audit already provides on the graph side
        (PHASE3_PLAN.md section 19).
        """
        distance = EmbeddingRow.embedding.cosine_distance(query_embedding)
        stmt = (
            self._select_columns(EmbeddingRow, EmbeddingRow.memory_id, distance.label("distance"))
            .where(
                EmbeddingRow.embedding_provider == embedding_provider,
                EmbeddingRow.embedding_model == embedding_model,
            )
            .order_by(distance)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            SimilarMemory(memory_id=row.memory_id, similarity=_similarity(row.distance))
            for row in result
        ]

    async def find_similar_to_memory(
        self,
        *,
        memory_id: str,
        embedding_provider: str,
        embedding_model: str,
        limit: int,
    ) -> list[SimilarMemory]:
        """Nearest neighbors to an *existing* memory's own stored embedding.

        Used by semantic duplicate detection (`app/semantic/similarity.py`'s
        caller in the extraction runner): "what does this newly-created memory
        semantically resemble" is exactly this query, self-excluding the memory
        via `!=` since the closest vector to a row is always itself at distance 0.
        Returns an empty list (never an error) if `memory_id` has no stored
        embedding yet -- a brand-new memory not yet embedded simply has nothing to
        compare, which the caller treats as "no semantic evidence available", not
        a failure.
        """
        subject = self._select_columns(EmbeddingRow, EmbeddingRow.embedding).where(
            EmbeddingRow.memory_id == memory_id,
            EmbeddingRow.embedding_provider == embedding_provider,
            EmbeddingRow.embedding_model == embedding_model,
        )
        result = await self._session.execute(subject)
        row = result.first()
        if row is None:
            return []

        distance = EmbeddingRow.embedding.cosine_distance(row.embedding)
        stmt = (
            self._select_columns(EmbeddingRow, EmbeddingRow.memory_id, distance.label("distance"))
            .where(
                EmbeddingRow.embedding_provider == embedding_provider,
                EmbeddingRow.embedding_model == embedding_model,
                EmbeddingRow.memory_id != memory_id,
            )
            .order_by(distance)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            SimilarMemory(memory_id=r.memory_id, similarity=_similarity(r.distance))
            for r in result
        ]
