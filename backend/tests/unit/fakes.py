"""In-memory test doubles.

These are **test fixtures, not product code**. Real persistence is Neo4j; nothing in
`app/` stores memory in a Python dict. They exist so that the service and API layers
-- where the lifecycle rules live -- can be tested exhaustively and deterministically
without a database, per the Phase 1 requirement that unit tests not need a running
server. The Cypher these doubles stand in for is covered by the integration suite
and by the static audit in `test_cypher_safety.py`.

The doubles deliberately reproduce two behaviours that matter for correctness:

* every lookup is scoped by `user_id`, so isolation tests are meaningful here too
* `create` is all-or-nothing, so a rejected supersession leaves no partial record
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.errors import NotFoundError
from app.models.domain import (
    Entity,
    EntityDetail,
    GraphNode,
    Memory,
    MemoryDetail,
    Neighborhood,
    Relationship,
    RelationshipView,
    ResolvedEntity,
)
from app.models.enums import Direction, MemoryStatus
from app.repositories.mapping import to_memory


class FakeMemoryRepository:
    """Stands in for MemoryRepository. Keyed by (user_id, memory_id)."""

    def __init__(self) -> None:
        self.memories: dict[tuple[str, str], dict[str, Any]] = {}
        self.entities: dict[tuple[str, str], Entity] = {}        # (user, normalized)
        self.mentions: dict[str, set[str]] = {}                  # memory_id -> normalized
        self.relationships: list[Relationship] = []
        # memory_id -> [source dict, ...]. Phase 2: many-to-many, deduplicated per
        # user on (source_type, dedup_key) -- the same key the real MERGE_SOURCE /
        # ATTACH_SOURCE Cypher uses.
        self.sources: dict[str, list[dict[str, Any]]] = {}
        self._source_nodes: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.supersessions: list[tuple[str, str]] = []           # (new_id, old_id)
        self.conflicts: list[tuple[str, str]] = []               # (memory_id_a, memory_id_b)

    async def create(
        self,
        *,
        user_id: str,
        memory_props: dict[str, Any],
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        source: dict[str, Any] | None,
        supersedes: list[str],
        now: datetime,
    ) -> None:
        # Validate the whole write before mutating anything, mirroring the real
        # repository's single transaction.
        missing = [m for m in supersedes if (user_id, m) not in self.memories]
        if missing:
            raise NotFoundError(
                "One or more memories to supersede do not exist",
                details={"missing_memory_ids": sorted(missing)},
            )

        memory_id = memory_props["id"]
        self.memories[(user_id, memory_id)] = dict(memory_props)

        normalized_names: set[str] = set()
        for ent in entities:
            key = (user_id, ent["normalized_name"])
            if key not in self.entities:
                self.entities[key] = Entity(
                    id=ent["id"],
                    user_id=user_id,
                    name=ent["name"],
                    normalized_name=ent["normalized_name"],
                    entity_type=ent["entity_type"],
                    aliases=list(ent["aliases"]),
                    created_at=now,
                    updated_at=now,
                )
            normalized_names.add(ent["normalized_name"])
        self.mentions[memory_id] = normalized_names

        for rel in relationships:
            props = rel["props"]
            self.relationships.append(
                Relationship(
                    id=props["id"],
                    user_id=user_id,
                    predicate=props["predicate"],
                    source_entity_id=self.entities[(user_id, rel["source_normalized"])].id,
                    target_entity_id=self.entities[(user_id, rel["target_normalized"])].id,
                    status=props["status"],
                    confidence=props["confidence"],
                    memory_id=props.get("memory_id"),
                    valid_from=props.get("valid_from"),
                    valid_until=props.get("valid_until"),
                    created_at=props["created_at"],
                    updated_at=props["updated_at"],
                )
            )

        if source:
            self._attach_source(user_id=user_id, memory_id=memory_id, source=source, now=now)

        for old_id in supersedes:
            old = self.memories[(user_id, old_id)]
            old["status"] = MemoryStatus.SUPERSEDED.value
            old["valid_until"] = old.get("valid_until") or now
            old["updated_at"] = now
            self.supersessions.append((memory_id, old_id))
            for rel in self.relationships:
                if rel.memory_id == old_id:
                    rel.status = MemoryStatus.SUPERSEDED
                    rel.valid_until = rel.valid_until or now
                    rel.updated_at = now

    async def get(self, *, user_id: str, memory_id: str) -> MemoryDetail | None:
        props = self.memories.get((user_id, memory_id))
        if props is None:
            return None
        normalized = self.mentions.get(memory_id, set())
        entities = [
            e for (uid, norm), e in self.entities.items()
            if uid == user_id and norm in normalized
        ]
        from app.models.domain import Source

        sources = [
            Source(
                id=s["id"],
                user_id=user_id,
                source_type=s["source_type"],
                reference=s.get("reference"),
                location=s.get("location"),
                created_at=s["created_at"],
            )
            for s in self.sources.get(memory_id, [])
        ]
        return MemoryDetail(
            memory=to_memory(props),
            entities=entities,
            relationships=[r for r in self.relationships if r.memory_id == memory_id],
            sources=sources,
            supersedes=[old for new, old in self.supersessions if new == memory_id],
            superseded_by=[new for new, old in self.supersessions if old == memory_id],
            conflicts_with=sorted(
                {b for a, b in self.conflicts if a == memory_id}
                | {a for a, b in self.conflicts if b == memory_id}
            ),
        )

    def _attach_source(
        self, *, user_id: str, memory_id: str, source: dict[str, Any], now: datetime
    ) -> None:
        """Mirrors ATTACH_SOURCE's MERGE dedup: same (source_type, dedup_key) for
        this user reuses the same Source node instead of creating a new one.
        """
        key = (user_id, source["source_type"], source.get("dedup_key", ""))
        existing = self._source_nodes.get(key)
        if existing is None:
            existing = {**source, "user_id": user_id, "created_at": now}
            self._source_nodes[key] = existing
        self.sources.setdefault(memory_id, [])
        if existing["id"] not in {s["id"] for s in self.sources[memory_id]}:
            self.sources[memory_id].append(existing)

    async def attach_source(
        self, *, user_id: str, memory_id: str, source: dict[str, Any], now: datetime
    ) -> None:
        if (user_id, memory_id) not in self.memories:
            raise NotFoundError("Memory not found", details={"memory_id": memory_id})
        self._attach_source(user_id=user_id, memory_id=memory_id, source=source, now=now)

    async def link_conflicting(
        self, *, user_id: str, memory_id_a: str, memory_id_b: str, now: datetime
    ) -> None:
        for mid in (memory_id_a, memory_id_b):
            if (user_id, mid) not in self.memories:
                raise NotFoundError("Memory not found", details={"memory_id": mid})
        pair = tuple(sorted((memory_id_a, memory_id_b)))
        if pair not in {tuple(sorted(p)) for p in self.conflicts}:
            self.conflicts.append(pair)

    async def update(
        self, *, user_id: str, memory_id: str, changes: dict[str, Any], now: datetime
    ) -> Memory | None:
        props = self.memories.get((user_id, memory_id))
        if props is None:
            return None
        for key, value in changes.items():
            if value is None:
                props.pop(key, None)
            else:
                props[key] = value
        props["updated_at"] = now
        return to_memory(props)

    async def invalidate(
        self,
        *,
        user_id: str,
        memory_id: str,
        reason: str | None,
        now: datetime,
        status: MemoryStatus = MemoryStatus.INVALID,
    ) -> Memory | None:
        props = self.memories.get((user_id, memory_id))
        if props is None:
            return None
        props["status"] = status.value
        props["valid_until"] = props.get("valid_until") or now
        props["updated_at"] = now
        if reason is not None:
            props["invalidation_reason"] = reason
        for rel in self.relationships:
            if rel.memory_id == memory_id:
                rel.status = status
                rel.valid_until = rel.valid_until or now
                rel.updated_at = now
        return to_memory(props)

    async def search(
        self, *, user_id: str, filters: dict[str, Any], limit: int, offset: int
    ) -> list[Memory]:
        rows = self._filtered(user_id, filters)
        rows.sort(key=lambda p: (p["created_at"], p["id"]), reverse=True)
        return [to_memory(p) for p in rows[offset : offset + limit]]

    async def count(self, *, user_id: str, filters: dict[str, Any]) -> int:
        return len(self._filtered(user_id, filters))

    def _filtered(self, user_id: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        from app.models.lifecycle import is_valid_at

        out: list[dict[str, Any]] = []
        for (uid, mid), props in self.memories.items():
            if uid != user_id:
                continue
            if (q := filters.get("query")) and q.lower() not in props["content"].lower():
                continue
            if (st := filters.get("statuses")) and props["status"] not in st:
                continue
            if (mt := filters.get("memory_types")) and props["memory_type"] not in mt:
                continue
            if (as_of := filters.get("as_of")) and not is_valid_at(
                props.get("valid_from"), props.get("valid_until"), as_of
            ):
                continue
            name = filters.get("entity_normalized_name")
            if name and name not in self.mentions.get(mid, set()):
                continue
            out.append(props)
        return out


class FakeEntityRepository:
    """Stands in for EntityRepository, reading from a FakeMemoryRepository's state."""

    def __init__(self, store: FakeMemoryRepository) -> None:
        self._store = store

    async def get(self, *, user_id: str, entity_id: str) -> EntityDetail | None:
        entity = self._by_id(user_id, entity_id)
        if entity is None:
            return None
        rel_count = sum(
            1
            for r in self._store.relationships
            if r.user_id == user_id
            and entity_id in (r.source_entity_id, r.target_entity_id)
        )
        normalized = entity.normalized_name
        mem_count = sum(
            1
            for (uid, mid), _ in self._store.memories.items()
            if uid == user_id and normalized in self._store.mentions.get(mid, set())
        )
        return EntityDetail(
            entity=entity, memory_count=mem_count, relationship_count=rel_count
        )

    async def find_by_normalized_name(
        self, *, user_id: str, normalized_name: str
    ) -> Entity | None:
        return self._store.entities.get((user_id, normalized_name))

    async def resolve(self, *, user_id: str, entity_id: str) -> ResolvedEntity | None:
        """This fake never models `MERGED_INTO` redirects -- every entity
        resolves to itself, matching `EntityRepository.resolve`'s contract
        for the no-redirect case exactly (real redirect behaviour is only
        verified against real Neo4j, in tests/integration).
        """
        entity = self._by_id(user_id, entity_id)
        if entity is None:
            return None
        return ResolvedEntity(
            entity=entity, original_id=entity_id, redirect_depth=0, was_redirected=False
        )

    async def search(
        self, *, user_id: str, query: str, limit: int, offset: int
    ) -> list[Entity]:
        matches = [
            e
            for (uid, _), e in self._store.entities.items()
            if uid == user_id
            and (
                query.lower() in e.name.lower()
                or any(query.lower() in a.lower() for a in e.aliases)
            )
        ]
        matches.sort(key=lambda e: e.name)
        return matches[offset : offset + limit]

    async def relationships(
        self,
        *,
        user_id: str,
        entity_id: str,
        direction: Direction,
        excluded_statuses: list[str],
    ) -> list[RelationshipView]:
        views: list[RelationshipView] = []
        for rel in self._store.relationships:
            if rel.user_id != user_id:
                continue
            if entity_id not in (rel.source_entity_id, rel.target_entity_id):
                continue
            if rel.status.value in excluded_statuses:
                continue
            if direction is Direction.OUTGOING and rel.source_entity_id != entity_id:
                continue
            if direction is Direction.INCOMING and rel.target_entity_id != entity_id:
                continue
            src = self._by_id(user_id, rel.source_entity_id)
            tgt = self._by_id(user_id, rel.target_entity_id)
            if src is None or tgt is None:
                continue
            views.append(
                RelationshipView(relationship=rel, source_entity=src, target_entity=tgt)
            )
        return views

    async def memories(
        self,
        *,
        user_id: str,
        entity_id: str,
        excluded_statuses: list[str],
        limit: int,
        offset: int,
    ) -> list[Memory]:
        entity = self._by_id(user_id, entity_id)
        if entity is None:
            return []
        rows = [
            props
            for (uid, mid), props in self._store.memories.items()
            if uid == user_id
            and entity.normalized_name in self._store.mentions.get(mid, set())
            and props["status"] not in excluded_statuses
        ]
        rows.sort(key=lambda p: (p["created_at"], p["id"]), reverse=True)
        return [to_memory(p) for p in rows[offset : offset + limit]]

    async def neighbourhood(
        self,
        *,
        user_id: str,
        root: Entity,
        hops: int,
        max_hops: int,
        excluded_statuses: list[str],
    ) -> Neighborhood:
        from app.repositories.entity_repository import validate_hops

        safe_hops = validate_hops(hops, max_hops)
        edges = [
            r
            for r in self._store.relationships
            if r.user_id == user_id and r.status.value not in excluded_statuses
        ]
        # Breadth-first expansion, recording the shortest hop distance per node.
        distance: dict[str, int] = {root.id: 0}
        frontier = {root.id}
        for depth in range(1, safe_hops + 1):
            nxt: set[str] = set()
            for edge in edges:
                for a, b in (
                    (edge.source_entity_id, edge.target_entity_id),
                    (edge.target_entity_id, edge.source_entity_id),
                ):
                    if a in frontier and b not in distance:
                        distance[b] = depth
                        nxt.add(b)
            frontier = nxt
            if not frontier:
                break
        nodes = [
            GraphNode(entity=e, hops=distance[e.id])
            for (uid, _), e in self._store.entities.items()
            if uid == user_id and e.id in distance and e.id != root.id
        ]
        nodes.sort(key=lambda n: (n.hops, n.entity.name))
        ids = set(distance)
        rels = [
            r
            for r in edges
            if r.source_entity_id in ids and r.target_entity_id in ids
        ]
        return Neighborhood(root=root, hops=safe_hops, nodes=nodes, relationships=rels)

    def _by_id(self, user_id: str, entity_id: str) -> Entity | None:
        for (uid, _), entity in self._store.entities.items():
            if uid == user_id and entity.id == entity_id:
                return entity
        return None
