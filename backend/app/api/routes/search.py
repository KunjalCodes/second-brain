"""Hybrid graph + vector + keyword search (ADR 0009, PHASE3_PLAN.md).

Handlers do no business logic: they bind HTTP to `HybridRetrievalService` and
nothing else, matching every other route module.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, HybridRetrievalServiceDep
from app.schemas.search import (
    RetrievalExplanationView,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse, summary="Hybrid graph + vector + keyword search")
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
                explanation=RetrievalExplanationView(**vars(r.explanation)),
            )
            for r in results
        ]
    )
