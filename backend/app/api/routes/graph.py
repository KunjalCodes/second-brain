"""Memory graph exploration API (Phase 8, docs/PHASE8_PLAN.md section 9).

Read-only, bounded slices of the requesting user's canonical graph for a
Memory Explorer. Every endpoint is `GET`, user-scoped through `CurrentUser`,
and never accepts Cypher, labels or relationship types from the client.
Budgets are clamped server-side (a client may lower them, never raise them);
another user's ids behave exactly like ids that do not exist (404, or no
effect in `exclude_ids`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import (
    RATE_LIMIT_GRAPH,
    CurrentUser,
    GraphExplorationServiceDep,
    GraphIntelligenceServiceDep,
)
from app.models.enums import EntityType
from app.models.normalization import PREDICATE_PATTERN
from app.schemas.graph import EdgeEvidence, GraphInclude, GraphSlice, TemporalScope
from app.schemas.graph_intelligence import (
    ContextRequest,
    ContextSubgraph,
    EdgeExplanation,
    PathsResponse,
)

router = APIRouter(prefix="/graph", tags=["graph"], dependencies=[RATE_LIMIT_GRAPH])

#: Stored ids are UUIDs; this accepts them (and other plain identifiers)
#: while keeping arbitrary text out of every id parameter.
ID_PATTERN = r"^[A-Za-z0-9_.:\-]{1,128}$"
MAX_PREDICATES = 20

GraphId = Annotated[str, Path(pattern=ID_PATTERN)]
Scope = Annotated[
    TemporalScope,
    Query(description="current (default) | as_of (requires as_of) | all | historical"),
]
AsOf = Annotated[
    datetime | None,
    Query(description="Required iff temporal_scope=as_of; with all, moves the labelling instant"),
]
Include = Annotated[list[GraphInclude] | None, Query(description="memories, merges")]
EntityTypes = Annotated[list[EntityType] | None, Query(description="Repeatable entity type filter")]


def _values(items: list | None) -> list[str]:
    return [i.value if hasattr(i, "value") else i for i in items or []]


@router.get("/overview", response_model=GraphSlice, summary="Bounded landing slice")
async def graph_overview(
    user_id: CurrentUser,
    service: GraphExplorationServiceDep,
    limit: Annotated[
        int | None, Query(ge=1, description="Clamped to GRAPH_EXPLORE_MAX_NODES")
    ] = None,
    max_edges: Annotated[
        int | None, Query(ge=1, description="Clamped to GRAPH_EXPLORE_MAX_EDGES")
    ] = None,
    temporal_scope: Scope = TemporalScope.CURRENT,
    as_of: AsOf = None,
    entity_type: EntityTypes = None,
    include: Include = None,
) -> GraphSlice:
    """The user's canonical entities, most recently updated first (ties: name,
    then id), merged-away entities excluded, plus the in-scope edges among them."""
    return await service.overview(
        user_id=user_id,
        limit=limit,
        max_edges=max_edges,
        temporal_scope=temporal_scope,
        as_of=as_of,
        entity_types=_values(entity_type),
        include=set(include or []),
    )


@router.get(
    "/entities/{entity_id}/neighborhood",
    response_model=GraphSlice,
    summary="Bounded, incremental neighborhood expansion",
)
async def graph_neighborhood(
    entity_id: GraphId,
    user_id: CurrentUser,
    service: GraphExplorationServiceDep,
    hops: Annotated[int, Query(ge=1, description="1..GRAPH_EXPLORE_MAX_HOPS")] = 1,
    max_nodes: Annotated[
        int | None, Query(ge=1, description="Clamped to GRAPH_EXPLORE_MAX_NODES")
    ] = None,
    max_edges: Annotated[
        int | None, Query(ge=1, description="Clamped to GRAPH_EXPLORE_MAX_EDGES")
    ] = None,
    temporal_scope: Scope = TemporalScope.CURRENT,
    as_of: AsOf = None,
    predicate: Annotated[
        list[Annotated[str, Query(pattern=PREDICATE_PATTERN, max_length=100)]] | None,
        Query(max_length=MAX_PREDICATES, description="Repeatable predicate filter"),
    ] = None,
    entity_type: EntityTypes = None,
    exclude_ids: Annotated[
        list[Annotated[str, Query(pattern=ID_PATTERN)]] | None,
        Query(description="Already-loaded node ids: neither returned nor re-expanded"),
    ] = None,
    include: Include = None,
) -> GraphSlice:
    """Resolves a merged-away id to its canonical entity (`resolved_from`
    reports the original). Temporal and status scope are applied on every hop."""
    return await service.neighborhood(
        user_id=user_id,
        entity_id=entity_id,
        hops=hops,
        max_nodes=max_nodes,
        max_edges=max_edges,
        temporal_scope=temporal_scope,
        as_of=as_of,
        predicates=list(predicate or []),
        entity_types=_values(entity_type),
        exclude_ids=list(exclude_ids or []),
        include=set(include or []),
    )


@router.get(
    "/edges/evidence",
    response_model=EdgeEvidence,
    summary="Relationship -> asserting memories -> provenance, in one call",
)
async def graph_edge_evidence(
    user_id: CurrentUser,
    service: GraphExplorationServiceDep,
    source_entity_id: Annotated[str, Query(pattern=ID_PATTERN)],
    target_entity_id: Annotated[str, Query(pattern=ID_PATTERN)],
    predicate: Annotated[str, Query(pattern=PREDICATE_PATTERN, max_length=100)],
    temporal_scope: Scope = TemporalScope.CURRENT,
    as_of: AsOf = None,
    limit: Annotated[int | None, Query(ge=1, description="Clamped to SEARCH_MAX_LIMIT")] = None,
) -> EdgeEvidence:
    """Directed `source -[predicate]-> target`. Pass `temporal_scope=all` to
    include superseded/historical assertions."""
    return await service.edge_evidence(
        user_id=user_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate=predicate,
        temporal_scope=temporal_scope,
        as_of=as_of,
        limit=limit,
    )


@router.get(
    "/memories/{memory_id}/timeline",
    response_model=GraphSlice,
    summary="A memory's supersession / duplicate / conflict chain, oldest first",
)
async def graph_memory_timeline(
    memory_id: GraphId,
    user_id: CurrentUser,
    service: GraphExplorationServiceDep,
) -> GraphSlice:
    return await service.memory_timeline(user_id=user_id, memory_id=memory_id)


# -- Phase 15: graph intelligence ------------------------------------------------------


@router.get(
    "/paths",
    response_model=PathsResponse,
    summary="How two entities are connected: bounded shortest paths with evidence",
)
async def graph_paths(
    user_id: CurrentUser,
    service: GraphIntelligenceServiceDep,
    source_entity_id: Annotated[str, Query(pattern=ID_PATTERN)],
    target_entity_id: Annotated[str, Query(pattern=ID_PATTERN)],
    max_hops: Annotated[int | None, Query(ge=1, description="Clamped to server cap")] = None,
    max_paths: Annotated[int | None, Query(ge=1, description="Clamped to server cap")] = None,
    temporal_scope: Scope = TemporalScope.CURRENT,
    as_of: AsOf = None,
    predicate: Annotated[
        list[Annotated[str, Query(pattern=PREDICATE_PATTERN, max_length=100)]] | None,
        Query(max_length=MAX_PREDICATES),
    ] = None,
) -> PathsResponse:
    """Every step is a stored relationship with its asserting memories. A path
    longer than one step is labelled `inferred_from_graph`: each step was
    stated, the connection through intermediate entities was not."""
    return await service.paths(
        user_id=user_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        max_hops=max_hops,
        max_paths=max_paths,
        temporal_scope=temporal_scope,
        as_of=as_of,
        predicates=predicate or [],
    )


@router.get(
    "/edges/explain",
    response_model=EdgeExplanation,
    summary="Why a relationship exists, and why it is (or is no longer) current",
)
async def graph_edge_explain(
    user_id: CurrentUser,
    service: GraphIntelligenceServiceDep,
    source_entity_id: Annotated[str, Query(pattern=ID_PATTERN)],
    target_entity_id: Annotated[str, Query(pattern=ID_PATTERN)],
    predicate: Annotated[str, Query(pattern=PREDICATE_PATTERN, max_length=100)],
    temporal_scope: Scope = TemporalScope.CURRENT,
    as_of: AsOf = None,
) -> EdgeExplanation:
    return await service.explain_edge(
        user_id=user_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate=predicate,
        temporal_scope=temporal_scope,
        as_of=as_of,
    )


@router.post(
    "/context",
    response_model=ContextSubgraph,
    summary="The smallest useful subgraph for a question, with evidence",
)
async def graph_context(
    body: ContextRequest,
    user_id: CurrentUser,
    service: GraphIntelligenceServiceDep,
) -> ContextSubgraph:
    """A POST so the question never appears in a URL or access log."""
    return await service.context_subgraph(
        user_id=user_id,
        query=body.query,
        temporal_scope=body.temporal_scope,
        as_of=body.as_of,
        max_nodes=body.max_nodes,
        max_evidence=body.max_evidence,
    )
