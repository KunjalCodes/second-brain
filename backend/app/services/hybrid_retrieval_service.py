"""Hybrid graph + vector + keyword retrieval (ADR 0009, PHASE3_PLAN.md sections
8-12).

```text
query
  |
  +-- vector retrieval   (pgvector, EmbeddingRepository.search)
  +-- graph retrieval    (entity-anchored: does the query name an existing
  |                        entity? if so, that entity's memories)
  +-- keyword retrieval  (Phase 1's CONTAINS-based MemoryService.search,
                           unchanged, kept and reframed as one channel)
  |
  v
candidate merge (dedupe by memory_id, keep every channel's score)
  |
  v
temporal filter (Neo4j-authoritative status/validity -- never a PostgreSQL copy)
  |
  v
rank (app.semantic.ranking, explicit linear formula)
  |
  v
top_k results, each with a RetrievalExplanation reporting only what was
actually computed
```

Core invariants this module exists to uphold (PHASE3_PLAN.md, user's Phase 3
approval message):

* Neo4j remains canonical; a `memory_embeddings` row is never trusted for
  status or temporal validity -- every candidate's `MemoryDetail` is read fresh
  from Neo4j before ranking.
* A vector-search failure degrades to graph/keyword-only retrieval, never an
  error that makes memory look nonexistent (`VectorSearchUnavailableError` is
  caught here specifically).
* Every query is user-scoped -- `EmbeddingRepository`/`MemoryService`/
  `RetrievalService` all require `user_id` and enforce it structurally.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.embeddings.base import EmbeddingClient
from app.models.domain import GraphPathStep, MemoryDetail
from app.models.enums import EXCLUDED_FROM_DEFAULT_RETRIEVAL, MemoryStatus
from app.models.lifecycle import is_currently_believed, is_valid_at
from app.repositories.embedding_repository import EmbeddingRepository
from app.semantic.query_understanding import QueryIntent, TemporalIntent, detect_temporal_intent
from app.semantic.ranking import RankingInput, RankingWeights, rank
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)

#: Exponential recency decay. A fixed, documented initial choice (not exposed as
#: a setting, to avoid an ever-growing configuration surface for a value with no
#: evaluation evidence yet to tune it against) -- PHASE3_PLAN.md section 17's
#: ablation studies can zero out RETRIEVAL_WEIGHT_RECENCY entirely to measure
#: whether recency matters at all before this constant itself is worth exposing.
_RECENCY_HALF_LIFE_DAYS = 30.0

#: A candidate below this cosine similarity is treated as "not found by vector
#: search" at all, even if it is technically the nearest available row (relevant
#: with very few memories, where "nearest" can still mean barely related). A
#: fixed, documented initial choice -- like `_RECENCY_HALF_LIFE_DAYS`, not
#: exposed as a setting until evaluation evidence justifies tuning it.
_MIN_VECTOR_SIMILARITY = 0.3

#: How many of the user's entities are scanned for a mention in the query text,
#: to find the graph-retrieval anchor(s). Mirrors the same bounded-scan pattern
#: as `app.extraction.runner._AMBIGUITY_SCAN_LIMIT`.
_ENTITY_MENTION_SCAN_LIMIT = 500

#: Statuses excluded from hybrid retrieval by default -- identical to Phase 1's
#: `RetrievalService.DEFAULT_EXCLUDED` (invalid/archived hidden unless asked for).
_DEFAULT_EXCLUDED_STATUSES = frozenset(EXCLUDED_FROM_DEFAULT_RETRIEVAL)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class GraphHit:
    """One memory's best graph-channel evidence (PHASE4_PLAN.md sections 1/4):
    the shortest real path from some query-mentioned anchor entity to the
    entity that mentions this memory, and the exponential-decay score that
    distance implies. "Best" = lowest hops -- when the same memory is
    reachable via more than one anchor or path, the shorter one wins (merge,
    not concatenate); `path`/`hops` are never fabricated, always the actual
    traversed path `EntityRepository.neighbourhood_with_paths` returned.
    """

    score: float
    hops: int
    path: list[GraphPathStep]


@dataclass(frozen=True)
class RetrievalExplanation:
    """Reports only signals actually computed -- PHASE3_PLAN.md section 11: "do
    not report a score unless it was actually calculated." A `None` field means
    that channel never found this candidate at all, not that it scored zero.
    """

    memory_id: str
    retrieved_via: list[str]
    vector_similarity: float | None
    graph_relevance: float | None
    keyword_match: float | None
    temporal_fit: float
    confidence: float
    final_score: float
    temporal_state: str
    #: Hop count and real traversed path behind `graph_relevance` -- `None`
    #: exactly when `graph_relevance` is `None` (PHASE4_PLAN.md section 13).
    graph_distance: int | None = None
    graph_path: list[GraphPathStep] | None = None


@dataclass(frozen=True)
class HybridSearchResult:
    memory: MemoryDetail
    explanation: RetrievalExplanation


def _temporal_state(status: MemoryStatus, valid_until: datetime | None, now: datetime) -> str:
    if status is MemoryStatus.SUPERSEDED:
        return "superseded"
    if status is MemoryStatus.INVALID:
        return "invalid"
    if status is MemoryStatus.ARCHIVED:
        return "archived"
    if status is MemoryStatus.CONFLICTING:
        return "conflicting"
    return "current" if is_currently_believed(status, valid_until, now) else "historical"


def _temporal_fit(
    intent: QueryIntent, *, status: MemoryStatus, valid_from: datetime | None,
    valid_until: datetime | None, now: datetime,
) -> float:
    """1.0 if this memory matches the query's temporal intent, 0.0 otherwise --
    never negative (PHASE3_PLAN.md section 8: absence of a signal is neutral).
    Always 0.0 for UNSPECIFIED -- no boost, and (see `_passes_temporal_filter`)
    no filtering either.
    """
    if intent.temporal is TemporalIntent.CURRENT:
        return 1.0 if is_currently_believed(status, valid_until, now) else 0.0
    if intent.temporal is TemporalIntent.HISTORICAL:
        return 0.0 if is_currently_believed(status, valid_until, now) else 1.0
    if intent.temporal is TemporalIntent.AS_OF:
        assert intent.as_of is not None
        return 1.0 if is_valid_at(valid_from, valid_until, intent.as_of) else 0.0
    return 0.0


def _passes_temporal_filter(
    intent: QueryIntent, *, status: MemoryStatus, valid_from: datetime | None,
    valid_until: datetime | None, now: datetime,
) -> bool:
    """Whether this query's temporal intent excludes this candidate outright.

    This is the core defense named explicitly in the Phase 3 approval: "the
    vector index must NOT decide whether a memory is current" -- a CURRENT query
    must never return a superseded memory just because its embedding scored
    highest; an AS_OF/HISTORICAL query must still be able to surface it.
    UNSPECIFIED filters nothing.
    """
    if intent.temporal is TemporalIntent.UNSPECIFIED:
        return True
    return (
        _temporal_fit(
            intent, status=status, valid_from=valid_from, valid_until=valid_until, now=now
        )
        == 1.0
    )


class HybridRetrievalService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        memory_service: MemoryService,
        retrieval_service: RetrievalService,
        embedding_client: EmbeddingClient,
        settings: Settings,
        now_fn: Callable[[], datetime] = _utcnow,
        enable_vector: bool = True,
        enable_graph: bool = True,
        enable_keyword: bool = True,
    ) -> None:
        self._session = session
        self._memory_service = memory_service
        self._retrieval_service = retrieval_service
        self._embeddings = embedding_client
        self._settings = settings
        self._now = now_fn
        # Per-channel disable switches -- NOT a production feature (there is no
        # setting or API surface for these): a testing/evaluation seam only, for
        # PHASE3_PLAN.md section 17's ablation studies ("Graph only", "Vector
        # only", ...), which need candidates *not discovered* by a channel, not
        # merely down-weighted in ranking.
        self._enable_vector = enable_vector
        self._enable_graph = enable_graph
        self._enable_keyword = enable_keyword

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int | None = None,
        intent_override: QueryIntent | None = None,
    ) -> list[HybridSearchResult]:
        """`intent_override` lets a caller state temporal intent explicitly
        (PHASE5_PLAN.md section 8: the agent's `search_memory` tool accepts an
        explicit `temporal_scope`) rather than relying solely on
        `detect_temporal_intent`'s text-pattern matching -- layered on top of,
        never replacing, the existing automatic detection used when omitted.
        The override is still only ever a value this service or its caller
        computed from a closed vocabulary (never free text), so temporal
        filtering remains something the retrieval/services layer decides, not
        something an LLM can assert about itself.
        """
        now = self._now()
        intent = intent_override or detect_temporal_intent(query, now=now)
        top_k = limit or self._settings.retrieval_top_k

        vector_hits = (
            await self._vector_candidates(user_id=user_id, query=query)
            if self._enable_vector
            else {}
        )
        keyword_hits = (
            await self._keyword_candidates(user_id=user_id, query=query)
            if self._enable_keyword
            else {}
        )
        graph_hits = (
            await self._graph_candidates(user_id=user_id, query=query)
            if self._enable_graph
            else {}
        )

        memory_ids = set(vector_hits) | set(keyword_hits) | set(graph_hits)
        candidates: list[RankingInput] = []
        details: dict[str, MemoryDetail] = {}
        for memory_id in memory_ids:
            try:
                detail = await self._memory_service.get(user_id=user_id, memory_id=memory_id)
            except NotFoundError:
                continue  # stale reference (e.g. a superseded id no longer resolvable)
            if detail.memory.status in _DEFAULT_EXCLUDED_STATUSES:
                continue
            if not _passes_temporal_filter(
                intent, status=detail.memory.status, valid_from=detail.memory.valid_from,
                valid_until=detail.memory.valid_until, now=now,
            ):
                continue

            details[memory_id] = detail
            age_days = max((now - detail.memory.created_at).total_seconds() / 86400.0, 0.0)
            graph_hit = graph_hits.get(memory_id)
            candidates.append(
                RankingInput(
                    memory_id=memory_id,
                    vector_similarity=vector_hits.get(memory_id),
                    graph_relevance=graph_hit.score if graph_hit is not None else None,
                    keyword_match=keyword_hits.get(memory_id),
                    recency_score=math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS),
                    temporal_fit=_temporal_fit(
                        intent, status=detail.memory.status, valid_from=detail.memory.valid_from,
                        valid_until=detail.memory.valid_until, now=now,
                    ),
                    confidence=detail.memory.confidence,
                )
            )

        weights = RankingWeights(
            vector=self._settings.retrieval_weight_vector,
            graph=self._settings.retrieval_weight_graph,
            keyword=self._settings.retrieval_weight_keyword,
            recency=self._settings.retrieval_weight_recency,
            temporal=self._settings.retrieval_weight_temporal,
            confidence=self._settings.retrieval_weight_confidence,
        )
        ranked = rank(candidates, weights, top_k=top_k)

        results = []
        for ranking_input, score in ranked:
            detail = details[ranking_input.memory_id]
            retrieved_via = [
                name
                for name, hits in (
                    ("vector", vector_hits), ("graph", graph_hits), ("keyword", keyword_hits),
                )
                if ranking_input.memory_id in hits
            ]
            graph_hit = graph_hits.get(ranking_input.memory_id)
            results.append(
                HybridSearchResult(
                    memory=detail,
                    explanation=RetrievalExplanation(
                        memory_id=ranking_input.memory_id,
                        retrieved_via=retrieved_via,
                        vector_similarity=ranking_input.vector_similarity,
                        graph_relevance=ranking_input.graph_relevance,
                        keyword_match=ranking_input.keyword_match,
                        temporal_fit=ranking_input.temporal_fit,
                        confidence=ranking_input.confidence,
                        final_score=score,
                        temporal_state=_temporal_state(
                            detail.memory.status, detail.memory.valid_until, now
                        ),
                        graph_distance=graph_hit.hops if graph_hit is not None else None,
                        graph_path=graph_hit.path if graph_hit is not None else None,
                    ),
                )
            )
        return results

    async def _vector_candidates(self, *, user_id: str, query: str) -> dict[str, float]:
        """`memory_id -> cosine similarity`. Empty (never an error) if the vector
        index/PostgreSQL is unavailable -- core invariant 6: degrade to
        graph/keyword retrieval rather than making the whole search fail.
        """
        try:
            embed_result = await self._embeddings.embed(
                text=query, timeout=self._settings.embedding_timeout_seconds
            )
            repo = EmbeddingRepository(self._session, user_id=user_id)
            hits = await repo.search(
                query_embedding=embed_result.vector,
                embedding_provider=self._settings.embedding_provider,
                embedding_model=self._settings.embedding_model_resolved,
                limit=self._settings.retrieval_top_k * 3,  # over-fetch before ranking
            )
            # pgvector's `ORDER BY ... LIMIT k` returns the k *nearest* rows
            # unconditionally -- with few memories, "nearest" can still mean
            # barely related. A floor keeps a near-random match from being
            # reported as "found by vector search" just because nothing closer
            # exists yet; the fixed constant is a stated initial choice pending
            # evaluation evidence (PHASE3_PLAN.md section 16), not asserted as
            # tuned.
            return {
                h.memory_id: h.similarity
                for h in hits
                if h.similarity >= _MIN_VECTOR_SIMILARITY
            }
        except AppError as exc:
            logger.warning(
                "Vector retrieval unavailable, degrading to graph/keyword only: %s", exc.message
            )
            return {}

    async def _keyword_candidates(self, *, user_id: str, query: str) -> dict[str, float]:
        """Phase 1's CONTAINS-based content search, reframed as one retrieval
        channel (PHASE3_PLAN.md section 8) rather than replaced. Score 1.0 for
        every hit -- an exact substring match is binary, not graded.
        """
        result = await self._memory_service.search(
            user_id=user_id, query=query, include_excluded=True,
            limit=self._settings.retrieval_top_k * 3, offset=0,
        )
        return {m.id: 1.0 for m in result.items}

    async def _graph_candidates(self, *, user_id: str, query: str) -> dict[str, GraphHit]:
        """Entity-anchored, bounded multi-hop retrieval (PHASE4_PLAN.md
        sections 1-4/14): does the query name an entity this user already
        has? If so, that entity (hop 0, score 1.0) and its neighbourhood out
        to `settings.retrieval_graph_hops` (default 2, hard max 3 via the
        setting's own bound) are the graph channel's candidates, each memory
        scored by `decay_rate ** hops` at the SHORTEST real path that reaches
        it -- never an LLM-generated explanation, never a learned scorer.

        "Merge, not concatenate" applies twice here: once across ENTITIES (the
        same entity reached from two different anchors, or at two different
        hop counts, keeps only its shortest path) and once across MEMORIES
        (the same memory mentioned by two different entities keeps only the
        evidence from whichever entity is closer) -- see `GraphHit`'s
        docstring for the latter.

        The memory lookup for every discovered entity is ONE batched Neo4j
        round trip (`get_memories_for_entities`), not one per node: the
        dense-graph benchmark (PHASE4_PLAN.md section 14) measured the
        earlier per-node design at 39-105s at 10,000 nodes/200 nodes-returned,
        almost entirely spent in that N+1 pattern rather than the traversal
        itself (which stayed under 1s at the same scale) -- so the fix here
        is batching the query, not capping fan-out; `retrieval_graph_max_nodes`
        remains the only explosion bound, unchanged.

        Every mention is resolved through `RetrievalService.resolve_entity` --
        the same one canonical resolver `get_entity`/`find_entity_by_name`
        use, not a second implementation -- before it becomes a traversal
        anchor: `old/merged entity name -> mention detection -> canonical
        resolution -> retrieval`. A query naming an old, merged-away entity
        therefore anchors on the CANONICAL entity's neighbourhood, exactly
        like a direct lookup of that old id/name would. This does not change
        merge semantics: the merged-away entity's own node, and any memory
        that mentions only it (never copied by a merge, which only copies
        RELATES_TO edges), are untouched and still exist -- they simply are
        not what this channel anchors on once a redirect exists, the same
        choice already made for `get_entity`/`find_entity_by_name`.
        """
        try:
            entities = await self._retrieval_service.list_all_entities(
                user_id=user_id, limit=_ENTITY_MENTION_SCAN_LIMIT
            )
        except AppError:
            return {}

        query_lower = query.lower()
        mentioned = [e for e in entities if e.normalized_name in query_lower]
        if not mentioned:
            return {}

        # entity_id -> (hops, path) for the best (shortest) path found to it,
        # across every mentioned anchor's own neighbourhood.
        best_paths: dict[str, tuple[int, list[GraphPathStep]]] = {}

        def _record(entity_id: str, hops: int, path: list[GraphPathStep]) -> None:
            existing = best_paths.get(entity_id)
            if existing is None or hops < existing[0]:
                best_paths[entity_id] = (hops, path)

        for mention in mentioned:
            try:
                resolved = await self._retrieval_service.resolve_entity(
                    user_id=user_id, entity_id=mention.id
                )
            except AppError:
                continue
            if resolved is None:
                continue  # should not happen (mention just came from this user's own list)
            anchor = resolved.entity
            _record(anchor.id, 0, [GraphPathStep(entity_id=anchor.id, entity_name=anchor.name)])
            try:
                neighbours = await self._retrieval_service.get_neighbourhood_with_paths(
                    user_id=user_id,
                    entity_id=anchor.id,
                    hops=self._settings.retrieval_graph_hops,
                    max_nodes=self._settings.retrieval_graph_max_nodes,
                )
            except AppError:
                continue
            for node in neighbours:
                _record(node.entity.id, node.hops, node.path)

        memories_by_entity = await self._retrieval_service.get_memories_for_entities(
            user_id=user_id,
            entity_ids=list(best_paths),
            limit_per_entity=50,
            include_excluded=True,
        )

        decay_rate = self._settings.retrieval_graph_decay_rate
        hits: dict[str, GraphHit] = {}
        for entity_id, (hops, path) in best_paths.items():
            score = decay_rate**hops
            for memory in memories_by_entity.get(entity_id, []):
                existing = hits.get(memory.id)
                if existing is None or hops < existing.hops:
                    hits[memory.id] = GraphHit(score=score, hops=hops, path=path)
        return hits
