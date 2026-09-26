"""Hybrid graph + vector + keyword search (ADR 0009, PHASE3_PLAN.md).

Handlers do no business logic: they bind HTTP to `HybridRetrievalService` and
nothing else, matching every other route module.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import (
    RATE_LIMIT_SEARCH,
    CurrentUser,
    HybridRetrievalServiceDep,
)
from app.schemas.search import (
    RetrievalExplanationView,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from app.services.hybrid_retrieval_service import RetrievalExplanation

router = APIRouter(prefix="/search", tags=["search"])


def _explanation_view(e: RetrievalExplanation) -> RetrievalExplanationView:
    """Only the public fields. `graph_distance`/`graph_path` are internal to the
    agent's tool output; spreading every dataclass field into this
    `extra="forbid"` model made any non-empty search a 500."""
    return RetrievalExplanationView(
        memory_id=e.memory_id,
        retrieved_via=e.retrieved_via,
        vector_similarity=e.vector_similarity,
        graph_relevance=e.graph_relevance,
        keyword_match=e.keyword_match,
        temporal_fit=e.temporal_fit,
        confidence=e.confidence,
        final_score=e.final_score,
        temporal_state=e.temporal_state,
    )


@router.post(
    "",
    dependencies=[RATE_LIMIT_SEARCH],
    response_model=SearchResponse,
    summary="Hybrid graph + vector + keyword search",
)
async def search(
    payload: SearchRequest,
    user_id: CurrentUser,
    service: HybridRetrievalServiceDep,
) -> SearchResponse:
    """Merge vector, graph (entity-anchored) and keyword retrieval, apply the
    query's detected temporal intent as a Neo4j-verified filter, and rank the
    result with the explicit linear formula -- never a vector-only or
    graph-only answer, and never a memory's currency decided by embedding
    similarity alone (ADR 0009).
    """
    results = await service.search(user_id=user_id, query=payload.query, limit=payload.limit)
    return SearchResponse(
        items=[
            SearchResultItem(
                memory=r.memory,
                explanation=_explanation_view(r.explanation),
            )
            for r in results
        ]
    )
