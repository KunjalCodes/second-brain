"""Entity and graph-retrieval endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RetrievalServiceDep, SettingsDep
from app.models.domain import (
    Entity,
    EntityDetail,
    EntityStateView,
    Memory,
    Neighborhood,
    RelationshipView,
)
from app.models.enums import Direction
from app.schemas.memory import EntityListResponse

router = APIRouter(prefix="/entities", tags=["entities"])


# Declared before /{entity_id} so these are not captured as ids.
@router.get("/search", response_model=EntityListResponse, summary="Search entities by name")
async def search_entities(
    user_id: CurrentUser,
    service: RetrievalServiceDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, description="Substring of name or alias")],
    limit: Annotated[int, Query(ge=1)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_merged: Annotated[
        bool,
        Query(description="Also return merged-away entities (hidden by default)"),
    ] = False,
) -> EntityListResponse:
    items = await service.search_entities(
        user_id=user_id,
        query=q,
        limit=min(limit, settings.search_max_limit),
        offset=offset,
        include_merged=include_merged,
    )
    return EntityListResponse(items=items, limit=limit, offset=offset)


@router.get("/by-name", response_model=Entity, summary="Resolve an exact entity name")
async def get_entity_by_name(
    user_id: CurrentUser,
    service: RetrievalServiceDep,
    name: Annotated[str, Query(min_length=1)],
) -> Entity:
    """Look up an entity by its normalized name.

    Phase 1 resolution is normalization plus explicit aliases only; fuzzy and
    semantic matching arrive in Phase 4.
    """
    return await service.find_entity_by_name(user_id=user_id, name=name)


@router.get("/{entity_id}", response_model=EntityDetail, summary="Get one entity")
async def get_entity(
    entity_id: str,
    user_id: CurrentUser,
    service: RetrievalServiceDep,
) -> EntityDetail:
    return await service.get_entity(user_id=user_id, entity_id=entity_id)


@router.get(
    "/{entity_id}/relationships",
    response_model=list[RelationshipView],
    summary="Relationships incident to an entity",
)
async def get_entity_relationships(
    entity_id: str,
    user_id: CurrentUser,
    service: RetrievalServiceDep,
    direction: Annotated[Direction, Query()] = Direction.BOTH,
    as_of: Annotated[
        datetime | None, Query(description="Only edges valid at this instant")
    ] = None,
    include_excluded: Annotated[bool, Query()] = False,
) -> list[RelationshipView]:
    return await service.get_relationships(
        user_id=user_id,
        entity_id=entity_id,
        direction=direction,
        as_of=as_of,
        include_excluded=include_excluded,
    )


@router.get(
    "/{entity_id}/memories",
    response_model=list[Memory],
    summary="Memories mentioning an entity",
)
async def get_entity_memories(
    entity_id: str,
    user_id: CurrentUser,
    service: RetrievalServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_excluded: Annotated[bool, Query()] = False,
) -> list[Memory]:
    return await service.get_entity_memories(
        user_id=user_id,
        entity_id=entity_id,
        limit=min(limit, settings.search_max_limit),
        offset=offset,
        include_excluded=include_excluded,
    )


@router.get(
    "/{entity_id}/history",
    response_model=EntityStateView,
    summary="Current vs historical state of an entity",
)
async def get_entity_history(
    entity_id: str,
    user_id: CurrentUser,
    service: RetrievalServiceDep,
    as_of: Annotated[
        datetime | None,
        Query(description="Defaults to now. Pass a past instant to see that state."),
    ] = None,
) -> EntityStateView:
    """Split an entity's relationships into what held at `as_of` and what had ended.

    This is the `currently_uses` / `previously_used` projection: both are derived
    from one predicate and its validity window, so asking for a past date returns
    the fact that was true then.
    """
    return await service.get_entity_state(
        user_id=user_id, entity_id=entity_id, as_of=as_of
    )


@router.get(
    "/{entity_id}/graph",
    response_model=Neighborhood,
    summary="N-hop subgraph around an entity",
)
async def get_entity_graph(
    entity_id: str,
    user_id: CurrentUser,
    service: RetrievalServiceDep,
    hops: Annotated[int, Query(ge=1, description="Bounded by GRAPH_MAX_HOPS")] = 2,
) -> Neighborhood:
    return await service.get_neighbourhood(
        user_id=user_id, entity_id=entity_id, hops=hops
    )
