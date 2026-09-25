"""Memory graph exploration: bounded, read-only slices of the user's canonical
graph for a Memory Explorer UI (Phase 8, docs/PHASE8_PLAN.md).

This service owns budget clamping, temporal-scope resolution and response
assembly. It reuses, never reimplements:

* entity resolution -- `RetrievalService.resolve_entity` (MERGED_INTO
  redirects: merge-chain safe, cycle-safe, user-scoped);
* the memory-history walk -- `app.services.memory_history`;
* provenance -- `ProvenanceService.resolve_sources`.

Nothing here writes to either store.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.models.domain import Entity, Memory, MemoryDetail
from app.repositories.graph_repository import EvidenceRow, GraphRepository, scope_params
from app.schemas.graph import (
    LABEL_MAX_CHARS,
    EdgeEvidence,
    EdgeKind,
    EvidenceAssertion,
    EvidenceMemory,
    EvidenceSource,
    EvidenceStatus,
    GraphBudget,
    GraphInclude,
    GraphSlice,
    GraphSliceEdge,
    GraphSliceNode,
    NodeKind,
    TemporalScope,
    TemporalState,
    temporal_state,
)
from app.services.memory_history import walk_memory_history
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService

#: Same bound as the agent's `get_memory_history` (app/agent/tools.py).
TIMELINE_MAX_CHAIN = 20

#: Evidence text (message content) is length-bounded; the full message is one
#: conversation read away.
EVIDENCE_TEXT_MAX_CHARS = 4000

_LINK_KINDS = {
    "SUPERSEDES": EdgeKind.SUPERSEDES,
    "CONFLICTS_WITH": EdgeKind.CONFLICTS_WITH,
    "DUPLICATE_OF": EdgeKind.DUPLICATE_OF,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clamp(requested: int | None, cap: int) -> int:
    """A client may lower a budget, never raise it past the server cap."""
    if requested is None:
        return cap
    return max(1, min(requested, cap))


def resolve_scope(
    scope: TemporalScope, as_of: datetime | None, now: datetime
) -> datetime:
    """The instant a slice is classified at. `as_of` is required iff
    `scope=as_of`; for `all` it optionally moves the reference instant of
    `temporal_state` labelling; for `current` it is rejected (current means now,
    and silently ignoring an explicit instant would misrepresent the slice)."""
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        # A naive instant would reach Neo4j as a LocalDateTime, which never
        # compares with a stored DateTime -- every fact would silently vanish.
        raise ValidationError("as_of must include a timezone offset (e.g. ...Z)")
    if scope is TemporalScope.AS_OF:
        if as_of is None:
            raise ValidationError("as_of is required when temporal_scope=as_of")
        return as_of
    if scope is TemporalScope.CURRENT and as_of is not None:
        raise ValidationError(
            "as_of is only valid with temporal_scope=as_of or temporal_scope=all"
        )
    return as_of if as_of is not None else now


def _entity_node(
    entity: Entity, *, hops: int | None = None, merged_from_count: int | None = None
) -> GraphSliceNode:
    return GraphSliceNode(
        id=entity.id,
        kind=NodeKind.ENTITY,
        label=entity.name,
        entity_type=entity.entity_type,
        aliases=list(entity.aliases),
        created_at=entity.created_at,
        hops=hops,
        merged_from_count=merged_from_count,
    )


def _memory_node(memory: Memory, *, at: datetime) -> GraphSliceNode:
    return GraphSliceNode(
        id=memory.id,
        kind=NodeKind.MEMORY,
        label=_truncate(memory.content, LABEL_MAX_CHARS),
        memory_type=memory.memory_type,
        status=memory.status,
        confidence=memory.confidence,
        valid_from=memory.valid_from,
        valid_until=memory.valid_until,
        temporal_state=temporal_state(memory.valid_from, memory.valid_until, at),
        created_at=memory.created_at,
    )


def relates_to_edge_id(
    source: str, predicate: str, target: str, status: str, state: str
) -> str:
    return f"relates_to:{source}:{predicate}:{target}:{status}:{state}"


class GraphExplorationService:
    def __init__(
        self,
        *,
        repository: GraphRepository,
        retrieval: RetrievalService,
        memory: MemoryService,
        provenance: ProvenanceService | None,
        settings: Settings,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repo = repository
        self._retrieval = retrieval
        self._memory = memory
        self._provenance = provenance
        self._settings = settings
        self._now = now_fn

    # -- shared assembly -------------------------------------------------------

    async def _attach(
        self,
        *,
        user_id: str,
        entity_nodes: list[GraphSliceNode],
        reach_ids: list[str],
        scope: dict,
        at: datetime,
        predicates: list[str],
        include: set[GraphInclude],
        max_nodes: int,
        max_edges: int,
        max_memories: int,
    ) -> tuple[list[GraphSliceNode], list[GraphSliceEdge], bool, bool]:
        """Edges among (and out of) a set of entity nodes, plus optional memory
        nodes and merge counts. Returns (nodes, edges, truncated_edges,
        truncated_memories)."""
        slice_ids = [n.id for n in entity_nodes]
        rows = await self._repo.slice_edges(
            user_id=user_id,
            slice_ids=slice_ids,
            reach_ids=reach_ids,
            scope=scope,
            predicates=predicates,
            limit=max_edges + 1,
        )
        truncated_edges = len(rows) > max_edges
        edges = [
            GraphSliceEdge(
                id=relates_to_edge_id(r.source, r.predicate, r.target, r.status, r.state),
                kind=EdgeKind.RELATES_TO,
                source=r.source,
                target=r.target,
                predicate=r.predicate,
                status=r.status,
                valid_from=r.valid_from,
                valid_until=r.valid_until,
                temporal_state=TemporalState(r.state),
                memory_ids=r.memory_ids,
                assertion_count=r.assertion_count,
            )
            for r in rows[:max_edges]
        ]

        if GraphInclude.MERGES in include:
            counts = await self._repo.merged_from_counts(user_id=user_id, entity_ids=slice_ids)
            for node in entity_nodes:
                node.merged_from_count = counts.get(node.id, 0)

        nodes = list(entity_nodes)
        truncated_memories = False
        if GraphInclude.MEMORIES in include:
            by_entity = await self._repo.slice_memories(
                user_id=user_id, entity_ids=slice_ids, scope=scope, per_entity=max_memories + 1
            )
            memory_nodes: dict[str, GraphSliceNode] = {}
            mentions: list[GraphSliceEdge] = []
            room = max_nodes - len(nodes)
            for entity_id in slice_ids:
                found = by_entity.get(entity_id, [])
                if len(found) > max_memories:
                    truncated_memories = True
                for memory in found[:max_memories]:
                    if memory.id not in memory_nodes:
                        if len(memory_nodes) >= room:
                            truncated_memories = True
                            continue
                        memory_nodes[memory.id] = _memory_node(memory, at=at)
                    mentions.append(
                        GraphSliceEdge(
                            id=f"mentions:{memory.id}:{entity_id}",
                            kind=EdgeKind.MENTIONS,
                            source=memory.id,
                            target=entity_id,
                        )
                    )
            nodes.extend(memory_nodes.values())
            edge_room = max_edges - len(edges)
            links = await self._repo.memory_links(
                user_id=user_id, memory_ids=list(memory_nodes), limit=max(edge_room, 0) + 1
            )
            extra = mentions + [
                GraphSliceEdge(
                    id=f"{_LINK_KINDS[kind].value}:{source}:{target}",
                    kind=_LINK_KINDS[kind],
                    source=source,
                    target=target,
                )
                for kind, source, target in links
            ]
            if len(extra) > edge_room:
                truncated_edges = True
            edges.extend(extra[: max(edge_room, 0)])
        return nodes, edges, truncated_edges, truncated_memories

    # -- endpoints ---------------------------------------------------------------

    async def neighborhood(
        self,
        *,
        user_id: str,
        entity_id: str,
        hops: int = 1,
        max_nodes: int | None = None,
        max_edges: int | None = None,
        temporal_scope: TemporalScope = TemporalScope.CURRENT,
        as_of: datetime | None = None,
        predicates: list[str] | None = None,
        entity_types: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        include: set[GraphInclude] | None = None,
    ) -> GraphSlice:
        s = self._settings
        if isinstance(hops, bool) or not isinstance(hops, int):
            raise ValidationError("hops must be an integer")
        if hops < 1 or hops > s.graph_explore_max_hops:
            raise ValidationError(
                f"hops must be between 1 and {s.graph_explore_max_hops}",
                details={"received": hops, "max_hops": s.graph_explore_max_hops},
            )
        node_budget = clamp(max_nodes, s.graph_explore_max_nodes)
        edge_budget = clamp(max_edges, s.graph_explore_max_edges)
        excluded = list(dict.fromkeys(exclude_ids or []))
        if len(excluded) > s.graph_explore_max_nodes:
            raise ValidationError(
                f"exclude_ids accepts at most {s.graph_explore_max_nodes} ids",
                details={"received": len(excluded)},
            )
        at = resolve_scope(temporal_scope, as_of, self._now())
        scope = scope_params(temporal_scope, at)
        include = include or set()

        resolved = await self._retrieval.resolve_entity(user_id=user_id, entity_id=entity_id)
        if resolved is None:
            raise NotFoundError("Entity not found", details={"entity_id": entity_id})
        root = resolved.entity

        # Breadth-first, one bounded query per hop. The node budget counts
        # entity nodes including the root; `limit=remaining+1` makes the
        # truncation flag exact.
        entity_nodes = [_entity_node(root, hops=0)]
        seen = [root.id, *excluded]
        frontier = [root.id]
        truncated_nodes = False
        for depth in range(1, hops + 1):
            remaining = node_budget - len(entity_nodes)
            if remaining <= 0:
                # Exact even at a full budget: probe for one more in-scope node.
                probe = await self._repo.expand_frontier(
                    user_id=user_id, frontier=frontier, seen=seen, scope=scope,
                    predicates=predicates or [], entity_types=entity_types or [], limit=1,
                )
                truncated_nodes = bool(probe)
                break
            found = await self._repo.expand_frontier(
                user_id=user_id,
                frontier=frontier,
                seen=seen,
                scope=scope,
                predicates=predicates or [],
                entity_types=entity_types or [],
                limit=remaining + 1,
            )
            if len(found) > remaining:
                truncated_nodes = True
                found = found[:remaining]
            entity_nodes.extend(_entity_node(e, hops=depth) for e in found)
            if truncated_nodes or not found:
                break
            frontier = [e.id for e in found]
            seen.extend(frontier)

        slice_ids = [n.id for n in entity_nodes]
        nodes, edges, truncated_edges, truncated_memories = await self._attach(
            user_id=user_id,
            entity_nodes=entity_nodes,
            reach_ids=[*slice_ids, *excluded],
            scope=scope,
            at=at,
            predicates=predicates or [],
            include=include,
            max_nodes=node_budget,
            max_edges=edge_budget,
            max_memories=s.graph_explore_max_memories_per_entity,
        )
        return GraphSlice(
            root=root.id,
            resolved_from=resolved.original_id if resolved.was_redirected else None,
            redirect_depth=resolved.redirect_depth,
            temporal_scope=temporal_scope,
            as_of=at,
            budget=GraphBudget(
                max_nodes=node_budget,
                max_edges=edge_budget,
                max_hops=hops,
                max_memories_per_entity=s.graph_explore_max_memories_per_entity
                if GraphInclude.MEMORIES in include
                else None,
            ),
            truncated_nodes=truncated_nodes,
            truncated_edges=truncated_edges,
            truncated_memories=truncated_memories,
            nodes=nodes,
            edges=edges,
        )

    async def overview(
        self,
        *,
        user_id: str,
        limit: int | None = None,
        max_edges: int | None = None,
        temporal_scope: TemporalScope = TemporalScope.CURRENT,
        as_of: datetime | None = None,
        entity_types: list[str] | None = None,
        include: set[GraphInclude] | None = None,
    ) -> GraphSlice:
        s = self._settings
        node_budget = clamp(limit, s.graph_explore_max_nodes)
        edge_budget = clamp(max_edges, s.graph_explore_max_edges)
        at = resolve_scope(temporal_scope, as_of, self._now())
        scope = scope_params(temporal_scope, at)
        include = include or set()

        ranked = await self._repo.overview_entities(
            user_id=user_id, scope=scope, entity_types=entity_types or [], limit=node_budget + 1
        )
        truncated_nodes = len(ranked) > node_budget
        entity_nodes = [_entity_node(e) for e in ranked[:node_budget]]
        slice_ids = [n.id for n in entity_nodes]
        nodes, edges, truncated_edges, truncated_memories = await self._attach(
            user_id=user_id,
            entity_nodes=entity_nodes,
            reach_ids=slice_ids,
            scope=scope,
            at=at,
            predicates=[],
            include=include,
            max_nodes=node_budget,
            max_edges=edge_budget,
            max_memories=s.graph_explore_max_memories_per_entity,
        )
        return GraphSlice(
            temporal_scope=temporal_scope,
            as_of=at,
            budget=GraphBudget(
                max_nodes=node_budget,
                max_edges=edge_budget,
                max_memories_per_entity=s.graph_explore_max_memories_per_entity
                if GraphInclude.MEMORIES in include
                else None,
            ),
            truncated_nodes=truncated_nodes,
            truncated_edges=truncated_edges,
            truncated_memories=truncated_memories,
            nodes=nodes,
            edges=edges,
        )

    async def edge_evidence(
        self,
        *,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        predicate: str,
        temporal_scope: TemporalScope = TemporalScope.CURRENT,
        as_of: datetime | None = None,
        limit: int | None = None,
    ) -> EdgeEvidence:
        if self._provenance is None:
            raise RuntimeError("edge_evidence requires a ProvenanceService")
        cap = self._settings.search_max_limit
        effective_limit = clamp(limit, cap)
        at = resolve_scope(temporal_scope, as_of, self._now())
        scope = scope_params(temporal_scope, at)

        source = await self._retrieval.resolve_entity(user_id=user_id, entity_id=source_entity_id)
        if source is None:
            raise NotFoundError("Entity not found", details={"entity_id": source_entity_id})
        target = await self._retrieval.resolve_entity(user_id=user_id, entity_id=target_entity_id)
        if target is None:
            raise NotFoundError("Entity not found", details={"entity_id": target_entity_id})

        rows = await self._repo.edge_evidence(
            user_id=user_id,
            source_entity_id=source.entity.id,
            target_entity_id=target.entity.id,
            predicate=predicate,
            scope=scope,
            limit=effective_limit + 1,
        )
        truncated = len(rows) > effective_limit
        rows = rows[:effective_limit]

        # One batched provenance resolution for every source of every memory.
        all_sources = list(
            {s.id: s for row in rows for s in row.sources}.values()
        )
        resolved_sources = await self._provenance.resolve_sources(
            user_id=user_id, sources=all_sources
        )
        by_source_id = {sp.source.id: sp for sp in resolved_sources}

        assertions: list[EvidenceAssertion] = []
        memory_nodes: dict[str, GraphSliceNode] = {}
        source_nodes: dict[str, GraphSliceNode] = {}
        derived: dict[str, GraphSliceEdge] = {}
        for row in rows:
            assertions.append(self._assertion(row, at=at, by_source_id=by_source_id))
            if row.memory is None:
                continue
            memory_nodes.setdefault(row.memory.id, _memory_node(row.memory, at=at))
            for src in row.sources:
                source_nodes.setdefault(
                    src.id,
                    GraphSliceNode(
                        id=src.id,
                        kind=NodeKind.SOURCE,
                        label=src.source_type.value
                        + (f": {src.reference}" if src.reference else ""),
                        source_type=src.source_type,
                        created_at=src.created_at,
                    ),
                )
                edge_id = f"derived_from:{row.memory.id}:{src.id}"
                derived.setdefault(
                    edge_id,
                    GraphSliceEdge(
                        id=edge_id, kind=EdgeKind.DERIVED_FROM, source=row.memory.id, target=src.id
                    ),
                )

        return EdgeEvidence(
            source_entity_id=source.entity.id,
            target_entity_id=target.entity.id,
            predicate=predicate,
            source_resolved_from=source.original_id if source.was_redirected else None,
            target_resolved_from=target.original_id if target.was_redirected else None,
            temporal_scope=temporal_scope,
            as_of=at,
            limit=effective_limit,
            truncated=truncated,
            evidence_available=any(
                a.evidence_status is EvidenceStatus.AVAILABLE for a in assertions
            ),
            assertions=assertions,
            nodes=[*memory_nodes.values(), *source_nodes.values()],
            edges=list(derived.values()),
        )

    @staticmethod
    def _assertion(row: EvidenceRow, *, at: datetime, by_source_id: dict) -> EvidenceAssertion:
        rel = row.relationship
        memory_id = rel.get("memory_id")
        memory_view: EvidenceMemory | None = None
        if memory_id is None:
            status = EvidenceStatus.NO_ASSERTING_MEMORY
        elif row.memory is None:
            status = EvidenceStatus.MEMORY_NOT_FOUND
        else:
            sources: list[EvidenceSource] = []
            for src in row.sources:
                sp = by_source_id[src.id]
                resolved = any(
                    x is not None for x in (sp.message, sp.document, sp.document_chunk)
                )
                sources.append(
                    EvidenceSource(
                        source=src,
                        resolved=resolved,
                        message_id=sp.message.id if sp.message else None,
                        message_role=sp.message.role.value if sp.message else None,
                        message_content=_truncate(sp.message.content, EVIDENCE_TEXT_MAX_CHARS)
                        if sp.message
                        else None,
                        document=sp.document,
                        document_chunk=sp.document_chunk,
                    )
                )
            if not sources:
                status = EvidenceStatus.NO_SOURCES
            elif any(s.resolved for s in sources):
                status = EvidenceStatus.AVAILABLE
            else:
                status = EvidenceStatus.SOURCES_UNRESOLVED
            m = row.memory
            memory_view = EvidenceMemory(
                id=m.id,
                content=m.content,
                memory_type=m.memory_type,
                status=m.status,
                confidence=m.confidence,
                valid_from=m.valid_from,
                valid_until=m.valid_until,
                temporal_state=temporal_state(m.valid_from, m.valid_until, at),
                created_at=m.created_at,
                sources=sources,
                **row.links,
            )
        return EvidenceAssertion(
            relationship_id=rel["id"],
            status=rel["status"],
            confidence=rel["confidence"],
            valid_from=rel.get("valid_from"),
            valid_until=rel.get("valid_until"),
            temporal_state=TemporalState(row.state),
            memory_id=memory_id,
            evidence_status=status,
            memory=memory_view,
        )

    async def memory_timeline(self, *, user_id: str, memory_id: str) -> GraphSlice:
        """The memory's supersession / duplicate / conflict chain, oldest
        first (`valid_from`, then `created_at`, then `id`), via the shared
        history walk. Every status is shown as stored -- SUPERSEDED, MERGED,
        INVALID, CONFLICTING and HISTORICAL are never collapsed -- and the
        links between chain members are returned as edges."""
        now = self._now()
        walk = await walk_memory_history(
            self._memory,
            user_id=user_id,
            memory_id=memory_id,
            max_chain=TIMELINE_MAX_CHAIN,
            follow_conflicts=True,
        )
        details = sorted(walk.details, key=_chronological)
        ids = {d.memory.id for d in details}
        edges: dict[str, GraphSliceEdge] = {}
        for d in details:
            for older in d.supersedes:
                if older in ids:
                    _add_link(edges, EdgeKind.SUPERSEDES, d.memory.id, older)
            for canonical in d.duplicate_of:
                if canonical in ids:
                    _add_link(edges, EdgeKind.DUPLICATE_OF, d.memory.id, canonical)
            for other in d.conflicts_with:
                if other in ids:
                    # Symmetric: one edge per pair, lower id first (deterministic).
                    a, b = sorted((d.memory.id, other))
                    _add_link(edges, EdgeKind.CONFLICTS_WITH, a, b)
        return GraphSlice(
            root=memory_id,
            temporal_scope=TemporalScope.ALL,
            as_of=now,
            budget=GraphBudget(max_nodes=TIMELINE_MAX_CHAIN),
            truncated_nodes=walk.truncated,
            nodes=[_memory_node(d.memory, at=now) for d in details],
            edges=sorted(edges.values(), key=lambda e: e.id),
        )


def _chronological(detail: MemoryDetail) -> tuple:
    m = detail.memory
    # A null valid_from sorts first ("since the beginning").
    return (
        m.valid_from is not None,
        m.valid_from or m.created_at,
        m.created_at,
        m.id,
    )


def _add_link(edges: dict[str, GraphSliceEdge], kind: EdgeKind, source: str, target: str) -> None:
    edge_id = f"{kind.value}:{source}:{target}"
    edges.setdefault(
        edge_id, GraphSliceEdge(id=edge_id, kind=kind, source=source, target=target)
    )
