"""Deterministic graph retrieval.

Phase 1 retrieval is purely structural: entity lookup, incident relationships,
N-hop traversal and temporal state projection. There is no embedding, no ranking and
no LLM involved -- those arrive in Phase 3 behind the same service boundary, so that
the graph core can be exercised and measured on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.core.errors import NotFoundError
from app.models.domain import (
    Entity,
    EntityDetail,
    EntityStateView,
    GraphNodeWithPath,
    Memory,
    Neighborhood,
    RelationshipView,
    ResolvedEntity,
)
from app.models.enums import EXCLUDED_FROM_DEFAULT_RETRIEVAL, Direction
from app.models.lifecycle import is_valid_at
from app.models.normalization import normalize_name
from app.repositories.entity_repository import EntityRepository


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Statuses never returned by ordinary retrieval.
DEFAULT_EXCLUDED: list[str] = sorted(s.value for s in EXCLUDED_FROM_DEFAULT_RETRIEVAL)


class RetrievalService:
    def __init__(
        self,
        repository: EntityRepository,
        *,
        max_hops: int,
        now_fn: Callable[[], datetime] = _utcnow,
        max_nodes: int = 1000,
        max_edges: int = 5000,
    ) -> None:
        self._repo = repository
        self._max_hops = max_hops
        self._now = now_fn
        #: Caps for `get_neighbourhood` only (GRAPH_MAX_NODES/EDGES, Phase 8 D1).
        self._max_nodes = max_nodes
        self._max_edges = max_edges

    async def get_entity(self, *, user_id: str, entity_id: str) -> EntityDetail:
        """Resolves `entity_id` through any `MERGED_INTO` redirect first
        (ADR 0010, PHASE4_PLAN.md section 9) -- a lookup by an old, merged-away
        id transparently returns the canonical entity's current detail (its
        full memory/relationship counts, including anything merged into it
        since), not the frozen snapshot the merged-away node was left with.
        """
        resolved = await self._repo.resolve(user_id=user_id, entity_id=entity_id)
        if resolved is None:
            raise NotFoundError("Entity not found", details={"entity_id": entity_id})
        found = await self._repo.get(user_id=user_id, entity_id=resolved.entity.id)
        if found is None:
            raise NotFoundError("Entity not found", details={"entity_id": entity_id})
        return found

    async def find_entity_by_name(self, *, user_id: str, name: str) -> Entity:
        """Same redirect resolution as `get_entity`, for a name-based lookup:
        a merged-away entity keeps its own original name (only the canonical
        entity gains it as an alias), so searching by that old name must
        still resolve to the canonical entity, not the merged-away node.
        """
        found = await self._repo.find_by_normalized_name(
            user_id=user_id, normalized_name=normalize_name(name)
        )
        if found is None:
            raise NotFoundError("Entity not found", details={"name": name})
        resolved = await self._repo.resolve(user_id=user_id, entity_id=found.id)
        return resolved.entity if resolved is not None else found

    async def resolve_entity(self, *, user_id: str, entity_id: str) -> ResolvedEntity | None:
        """The one canonical entity-resolution mechanism (ADR 0010,
        PHASE4_PLAN.md section 9), exposed at the service layer so every
        consumer -- `get_entity`/`find_entity_by_name` above, and hybrid
        retrieval's graph channel -- resolves redirects the same one way.
        A thin pass-through to `EntityRepository.resolve`, not a
        reimplementation: `None` only if `entity_id` does not exist at all
        for this user.
        """
        return await self._repo.resolve(user_id=user_id, entity_id=entity_id)

    async def search_entities(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        offset: int,
        include_merged: bool = False,
    ) -> list[Entity]:
        """Merged-away entities are hidden by default (Phase 8, D2)."""
        return await self._repo.search(
            user_id=user_id,
            query=query,
            limit=limit,
            offset=offset,
            include_merged=include_merged,
        )

    async def list_all_entities(self, *, user_id: str, limit: int) -> list[Entity]:
        """Every entity for a user, bounded -- used by ambiguity detection
        (app/extraction/resolution.py), not by any search-box endpoint.
        """
        return await self._repo.list_all(user_id=user_id, limit=limit)

    async def get_relationships(
        self,
        *,
        user_id: str,
        entity_id: str,
        direction: Direction = Direction.BOTH,
        as_of: datetime | None = None,
        include_excluded: bool = False,
    ) -> list[RelationshipView]:
        """Relationships incident to an entity, optionally as of a past instant."""
        await self.get_entity(user_id=user_id, entity_id=entity_id)
        views = await self._repo.relationships(
            user_id=user_id,
            entity_id=entity_id,
            direction=direction,
            excluded_statuses=[] if include_excluded else DEFAULT_EXCLUDED,
        )
        if as_of is None:
            return views
        return [
            v
            for v in views
            if is_valid_at(
                v.relationship.valid_from, v.relationship.valid_until, as_of
            )
        ]

    async def get_entity_memories(
        self,
        *,
        user_id: str,
        entity_id: str,
        limit: int,
        offset: int,
        include_excluded: bool = False,
    ) -> list[Memory]:
        await self.get_entity(user_id=user_id, entity_id=entity_id)
        return await self._repo.memories(
            user_id=user_id,
            entity_id=entity_id,
            excluded_statuses=[] if include_excluded else DEFAULT_EXCLUDED,
            limit=limit,
            offset=offset,
        )

    async def get_memories_for_entities(
        self,
        *,
        user_id: str,
        entity_ids: list[str],
        limit_per_entity: int,
        include_excluded: bool = False,
    ) -> dict[str, list[Memory]]:
        """Batched counterpart to `get_entity_memories` for many entities in
        one round trip (PHASE4_PLAN.md section 14) -- used by hybrid
        retrieval's graph channel, which used to call `get_entity_memories`
        once per neighbourhood node and, at scale, paid for it in latency
        (see the dense-graph benchmark). Does not raise for an unknown entity
        id -- it is simply absent from the result.
        """
        return await self._repo.memories_for_entities(
            user_id=user_id,
            entity_ids=entity_ids,
            excluded_statuses=[] if include_excluded else DEFAULT_EXCLUDED,
            limit_per_entity=limit_per_entity,
        )

    async def get_neighbourhood(
        self, *, user_id: str, entity_id: str, hops: int
    ) -> Neighborhood:
        """N-hop subgraph around an entity (CLAUDE.md section 19, multi-hop retrieval)."""
        detail = await self.get_entity(user_id=user_id, entity_id=entity_id)
        return await self._repo.neighbourhood(
            user_id=user_id,
            root=detail.entity,
            hops=hops,
            max_hops=self._max_hops,
            excluded_statuses=DEFAULT_EXCLUDED,
            max_nodes=self._max_nodes,
            max_edges=self._max_edges,
        )

    async def get_neighbourhood_with_paths(
        self, *, user_id: str, entity_id: str, hops: int, max_nodes: int
    ) -> list[GraphNodeWithPath]:
        """Same N-hop discovery as `get_neighbourhood`, but with the real
        shortest path captured per node (PHASE4_PLAN.md sections 1/4) -- used
        by hybrid retrieval's graph channel, not the interactive
        `GET /entities/{id}/neighbourhood` endpoint (which is unaffected).
        """
        detail = await self.get_entity(user_id=user_id, entity_id=entity_id)
        return await self._repo.neighbourhood_with_paths(
            user_id=user_id,
            root=detail.entity,
            hops=hops,
            max_hops=self._max_hops,
            excluded_statuses=DEFAULT_EXCLUDED,
            max_nodes=max_nodes,
        )

    async def get_entity_state(
        self, *, user_id: str, entity_id: str, as_of: datetime | None = None
    ) -> EntityStateView:
        """Split an entity's relationships into current and historical at an instant.

        This is the projection PRODUCT.md section 9 calls `currently_uses` /
        `previously_used`. Both buckets come from the *same* predicate plus its
        validity window rather than from two stored predicates:

        * **current**    -- the window contains `as_of`
        * **historical** -- the window closed at or before `as_of`

        An edge whose window starts after `as_of` appears in neither bucket: as of
        that instant it was not yet true and had no history.

        Because a superseded edge keeps its original window, asking for March
        correctly returns the fact that was current in March even though it has since
        been replaced -- which is the question CLAUDE.md section 15 requires to work.
        """
        detail = await self.get_entity(user_id=user_id, entity_id=entity_id)
        at = as_of if as_of is not None else self._now()
        views = await self._repo.relationships(
            user_id=user_id,
            entity_id=entity_id,
            direction=Direction.BOTH,
            excluded_statuses=DEFAULT_EXCLUDED,
        )

        current: list[RelationshipView] = []
        historical: list[RelationshipView] = []
        for view in views:
            rel = view.relationship
            if is_valid_at(rel.valid_from, rel.valid_until, at):
                current.append(view)
            elif rel.valid_until is not None and rel.valid_until <= at:
                historical.append(view)

        return EntityStateView(
            entity=detail.entity, as_of=at, current=current, historical=historical
        )
