"""Graph intelligence over the existing memory graph (Phase 15).

Bounded, read-only projections -- paths, relationship explanations, entity
context, memory evolution and query context subgraphs -- assembled from the
same stored records as the Phase 8 graph API:

* every relationship shown is a STORED assertion (aggregated per
  source/predicate/target/status/state, as the Phase 8 slices do);
* a connection through intermediate entities is labelled
  `inferred_from_graph`, never presented as stated;
* causality is only ever the predicate the memory stated (`RelationKind`);
  nothing here infers it from timing or co-occurrence;
* every traversal is breadth-first from Python, one bounded query per hop,
  with server-side depth/node/path caps a client may lower but never raise;
* every read is user-scoped; a foreign or unknown id is a 404.

Relevance scores are HEURISTIC and fully explained: each score is returned
with the named signals it was computed from (docs/PHASE15_GRAPH_INTELLIGENCE.md).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.models.domain import Entity, MemoryDetail, ResolvedEntity, Source
from app.models.enums import BELIEVED_STATUSES, MemoryStatus
from app.models.extraction import DecisionType
from app.models.lifecycle import is_valid_at
from app.repositories.extraction_repository import ExtractionCandidateRepository
from app.repositories.graph_repository import (
    AggregatedEdge,
    GraphRepository,
    scope_params,
)
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.review_audit_repository import ReviewAuditRepository
from app.schemas.graph import (
    EdgeKind,
    GraphSliceEdge,
    GraphSliceNode,
    NodeKind,
    TemporalScope,
    TemporalState,
    temporal_state,
)
from app.schemas.graph_intelligence import (
    ContextMemory,
    ContextRelationship,
    ContextSubgraph,
    EdgeExplanation,
    EntityContext,
    EvidenceBasis,
    EvidenceItem,
    EvolutionEvent,
    EvolutionKind,
    GraphPath,
    MemoryEvolution,
    MergedEntityRef,
    PathsResponse,
    PathStep,
    RelatedEntity,
    RelationKind,
    SourceRef,
    StepRelationship,
    SupportingMemory,
    TimelineEvent,
)
from app.semantic.query_understanding import QueryIntent, TemporalIntent, detect_temporal_intent
from app.services.graph_exploration_service import (
    TIMELINE_MAX_CHAIN,
    clamp,
    relates_to_edge_id,
    resolve_scope,
)
from app.services.memory_history import walk_memory_history
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService

#: Predicates that STATE a cause or consequence (M7). Only these are ever
#: labelled causal; the label reflects what was said, not a conclusion.
CAUSAL_PREDICATES = frozenset(
    {
        "led_to",
        "caused",
        "causes",
        "fixed_by",
        "fixed",
        "fixes",
        "resulted_in",
        "results_in",
        "because_of",
        "motivated_by",
        "made_for",
        "introduced",
        "introduced_by",
        "resolved_by",
        "resolves",
        "broke",
        "broken_by",
        "triggered",
        "triggered_by",
        "prevented",
        "enabled",
    }  # fmt: skip
)
#: Predicates that state a cause as UNCERTAIN -- kept distinct from causal.
POSSIBLE_CAUSE_PREDICATES = frozenset(
    {"possible_cause", "possibly_caused", "may_have_caused", "suspected_cause", "might_cause"}
)

#: Hops needed for "User -> Project -> Technology -> Library".
PATH_HARD_MAX_HOPS = 4
_MAX_SUPPORTING_MEMORIES = 10
_EVOLUTION_MAX_EVENTS = 60


def relation_kind(predicate: str) -> RelationKind:
    if predicate in POSSIBLE_CAUSE_PREDICATES:
        return RelationKind.POSSIBLE_CAUSE
    if predicate in CAUSAL_PREDICATES:
        return RelationKind.CAUSAL
    return RelationKind.ASSOCIATION


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _believed(status: str) -> bool:
    return MemoryStatus(status) in BELIEVED_STATUSES


def _edge_basis(edge: AggregatedEdge, scope: TemporalScope) -> list[EvidenceBasis]:
    basis = [EvidenceBasis.DIRECTLY_STATED]
    if len(set(edge.memory_ids)) > 1 or edge.assertion_count > 1:
        basis.append(EvidenceBasis.SUPPORTED_BY_MULTIPLE_MEMORIES)
    if scope is not TemporalScope.CURRENT and not (
        edge.state == TemporalState.CURRENT.value and _believed(edge.status)
    ):
        basis.append(EvidenceBasis.TEMPORALLY_DERIVED)
    return basis


def _source_ref(src: Source, resolved: bool | None) -> SourceRef:
    return SourceRef(
        source_type=src.source_type, reference=src.reference, location=src.location,
        resolved=resolved,
    )  # fmt: skip


class GraphIntelligenceService:
    def __init__(
        self,
        *,
        repository: GraphRepository,
        retrieval: RetrievalService,
        memory: MemoryService,
        provenance: ProvenanceService,
        session: AsyncSession,
        settings: Settings,
        hybrid: Any = None,  # HybridRetrievalService (context subgraphs only)
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repo = repository
        self._retrieval = retrieval
        self._memory = memory
        self._provenance = provenance
        self._session = session
        self._settings = settings
        self._hybrid = hybrid
        self._now = now_fn

    # -- shared -----------------------------------------------------------------

    async def _resolve(self, user_id: str, entity_id: str) -> ResolvedEntity:
        resolved = await self._retrieval.resolve_entity(user_id=user_id, entity_id=entity_id)
        if resolved is None:
            raise NotFoundError("Entity not found", details={"entity_id": entity_id})
        return resolved

    async def _sources(self, user_id: str, sources: list[Source]) -> list[SourceRef]:
        if not sources:
            return []
        resolved = await self._provenance.resolve_sources(user_id=user_id, sources=sources)
        return [_source_ref(sp.source, sp.resolved) for sp in resolved]

    # -- paths (M3, M4) ---------------------------------------------------------

    async def paths(
        self,
        *,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        max_hops: int | None = None,
        max_paths: int | None = None,
        temporal_scope: TemporalScope = TemporalScope.CURRENT,
        as_of: datetime | None = None,
        predicates: list[str] | None = None,
    ) -> PathsResponse:
        s = self._settings
        hops = clamp(max_hops, min(s.graph_path_max_hops, PATH_HARD_MAX_HOPS))
        path_cap = clamp(max_paths, s.graph_path_max_paths)
        at = resolve_scope(temporal_scope, as_of, self._now())
        scope = scope_params(temporal_scope, at)
        src = await self._resolve(user_id, source_entity_id)
        dst = await self._resolve(user_id, target_entity_id)
        names = {src.entity.id: src.entity.name, dst.entity.id: dst.entity.name}
        self.last_path_entities: dict[str, Entity] = {
            src.entity.id: src.entity,
            dst.entity.id: dst.entity,
        }

        def response(paths: list[GraphPath], reason: str | None, explored: int, trunc: bool):  # noqa: ANN202
            return PathsResponse(
                source_entity_id=src.entity.id, target_entity_id=dst.entity.id,
                source_resolved_from=src.original_id if src.was_redirected else None,
                target_resolved_from=dst.original_id if dst.was_redirected else None,
                temporal_scope=temporal_scope, as_of=at, max_hops=hops, max_paths=path_cap,
                paths=paths, reason=reason, explored_nodes=explored, truncated=trunc,
            )  # fmt: skip

        if src.entity.id == dst.entity.id:
            return response([], "same_entity", 1, False)

        entity_paths, explored, truncated = await self._shortest_entity_paths(
            user_id=user_id, source=src.entity.id, target=dst.entity.id, hops=hops,
            max_paths=path_cap, scope=scope, predicates=predicates or [], names=names,
        )  # fmt: skip
        if not entity_paths:
            reason = "node_budget_exhausted" if truncated else "not_connected_within_hops"
            return response([], reason, explored, truncated)
        built = await self._build_paths(
            user_id=user_id, entity_paths=entity_paths, names=names, scope=scope,
            temporal_scope=temporal_scope, predicates=predicates or [],
        )  # fmt: skip
        return response(built[:path_cap], None, explored, truncated)

    async def _shortest_entity_paths(
        self,
        *,
        user_id: str,
        source: str,
        target: str,
        hops: int,
        max_paths: int,
        scope: dict,
        predicates: list[str],
        names: dict[str, str],
    ) -> tuple[list[list[str]], int, bool]:
        """All shortest entity paths source -> target (up to `max_paths`).

        Bidirectional breadth-first search: each round expands the SMALLER of
        the two frontiers by one layer (one bounded query), and stops at the
        first layer where the two searches meet. On dense graphs this visits
        far fewer nodes than searching from one end -- measured: one-sided
        search exhausted the 150-node budget at 1,000+ synthetic entities
        (eval/graph_intelligence_benchmark.py). Combined depth <= `hops`; all
        visited nodes on both sides count against `graph_explore_max_nodes`.
        Undirected: a stored edge in either direction connects two entities
        (its direction is reported per step)."""
        budget = self._settings.graph_explore_max_nodes
        sides = {
            "f": {"parents": {source: []}, "depth": {source: 0}, "frontier": [source]},
            "b": {"parents": {target: []}, "depth": {target: 0}, "frontier": [target]},
        }
        truncated = False
        meet: set[str] = set()
        total_depth = 0
        while total_depth < hops and not meet:
            # Expand the side with the smaller total degree (a hub on one end
            # would otherwise spend the whole node budget in one layer).
            degree_f = await self._repo.frontier_degree(user_id=user_id, ids=sides["f"]["frontier"])
            degree_b = await self._repo.frontier_degree(user_id=user_id, ids=sides["b"]["frontier"])
            if not sides["b"]["frontier"]:
                key = "f"
            elif not sides["f"]["frontier"]:
                key = "b"
            else:
                key = "f" if degree_f <= degree_b else "b"
            side, other = sides[key], sides["b" if key == "f" else "f"]
            if not side["frontier"]:
                break
            visited = len(sides["f"]["parents"]) + len(sides["b"]["parents"])
            remaining = budget - visited
            if remaining <= 0:
                truncated = True
                break
            pair_limit = remaining * 4 + 1
            rows = await self._repo.expand_with_parents(
                user_id=user_id, frontier=side["frontier"], seen=list(side["parents"]),
                scope=scope, predicates=predicates, limit=pair_limit,
            )  # fmt: skip
            if len(rows) >= pair_limit:
                truncated = True
            layer: dict[str, list[str]] = {}
            for parent, entity in rows:
                if entity.id not in layer:
                    if len(layer) >= remaining:
                        truncated = True
                        continue
                    layer[entity.id] = []
                    names[entity.id] = entity.name
                    self.last_path_entities[entity.id] = entity
                layer[entity.id].append(parent)
            total_depth += 1
            if not layer:
                break
            depth = max(side["depth"][p] for p in side["frontier"]) + 1
            for child, ps in layer.items():
                side["parents"][child] = sorted(set(ps))
                side["depth"][child] = depth
            side["frontier"] = list(layer)
            meet = set(layer) & set(other["parents"])
        visited = len(set(sides["f"]["parents"]) | set(sides["b"]["parents"]))
        if not meet:
            return [], visited, truncated
        fdepth, bdepth = sides["f"]["depth"], sides["b"]["depth"]
        best = min(fdepth[m] + bdepth[m] for m in meet)
        meet = {m for m in meet if fdepth[m] + bdepth[m] == best}
        cap = max_paths * 4

        def chains(parents: dict[str, list[str]], node: str, root: str) -> list[list[str]]:
            """Every root..node chain through `parents` (bounded by `cap`)."""
            if node == root:
                return [[root]]
            out: list[list[str]] = []
            for p in parents[node]:
                for c in chains(parents, p, root):
                    out.append([*c, node])
                    if len(out) >= cap:
                        return out
            return out

        paths: list[list[str]] = []
        for m in sorted(meet):
            for head in chains(sides["f"]["parents"], m, source):
                for tail in chains(sides["b"]["parents"], m, target):
                    paths.append(head + list(reversed(tail))[1:])
                    if len(paths) >= cap:
                        break
        unique = sorted({tuple(p) for p in paths})
        return [list(p) for p in unique][:cap], visited, truncated

    async def _build_paths(
        self,
        *,
        user_id: str,
        entity_paths: list[list[str]],
        names: dict[str, str],
        scope: dict,
        temporal_scope: TemporalScope,
        predicates: list[str],
    ) -> list[GraphPath]:
        pairs = sorted({(p[i], p[i + 1]) for p in entity_paths for i in range(len(p) - 1)})
        rows = await self._repo.pair_edges(
            user_id=user_id, pairs=pairs, scope=scope, predicates=predicates,
            limit=len(pairs) * 12 + 1,
        )  # fmt: skip
        by_pair: dict[tuple[str, str], list[AggregatedEdge]] = {}
        for a, b, edge in rows:
            by_pair.setdefault((a, b), []).append(edge)
        paths: list[GraphPath] = []
        for ids in entity_paths:
            steps = []
            for a, b in zip(ids, ids[1:], strict=False):
                edges = by_pair.get((a, b), [])
                steps.append(
                    PathStep(
                        from_entity_id=a, from_name=names.get(a, a),
                        to_entity_id=b, to_name=names.get(b, b),
                        relationships=[
                            StepRelationship(
                                predicate=e.predicate,
                                direction="forward" if e.source == a else "backward",
                                relation_kind=relation_kind(e.predicate),
                                status=MemoryStatus(e.status),
                                temporal_state=TemporalState(e.state),
                                valid_from=e.valid_from, valid_until=e.valid_until,
                                assertion_count=e.assertion_count, confidence=e.confidence,
                                memory_ids=e.memory_ids, basis=_edge_basis(e, temporal_scope),
                            )
                            for e in edges
                        ],
                    )
                )  # fmt: skip
            if any(not st.relationships for st in steps):
                continue  # an edge fell out of scope between the two queries
            length = len(steps)
            support = min(max(r.assertion_count for r in st.relationships) for st in steps)
            current = (
                sum(
                    any(r.temporal_state is TemporalState.CURRENT for r in st.relationships)
                    for st in steps
                )
                / length
            )
            confidence = min(
                max((r.confidence if r.confidence is not None else 0.5) for r in st.relationships)
                for st in steps
            )
            signals = {
                "length": float(length),
                "min_support": float(support),
                "current_fraction": round(current, 3),
                "min_confidence": round(confidence, 3),
            }
            score = round(
                0.4 / length + 0.2 * min(support, 3) / 3 + 0.25 * current + 0.15 * confidence, 4
            )
            paths.append(
                GraphPath(
                    entity_ids=ids, length=length, steps=steps,
                    basis=EvidenceBasis.DIRECTLY_STATED if length == 1
                    else EvidenceBasis.INFERRED_FROM_GRAPH,
                    signals=signals, score=score,
                )
            )  # fmt: skip
        paths.sort(key=lambda p: (-p.score, p.entity_ids))
        return paths

    # -- relationship explanation (M11) -----------------------------------------

    async def explain_edge(
        self,
        *,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        predicate: str,
        temporal_scope: TemporalScope = TemporalScope.CURRENT,
        as_of: datetime | None = None,
    ) -> EdgeExplanation:
        at = resolve_scope(temporal_scope, as_of, self._now())
        scope = scope_params(temporal_scope, at)
        src = await self._resolve(user_id, source_entity_id)
        dst = await self._resolve(user_id, target_entity_id)
        rows = await self._repo.edge_evidence(
            user_id=user_id, source_entity_id=src.entity.id, target_entity_id=dst.entity.id,
            predicate=predicate, scope=scope, limit=_MAX_SUPPORTING_MEMORIES + 1,
        )  # fmt: skip
        if not rows:
            raise NotFoundError(
                "No such relationship in this time scope",
                details={"predicate": predicate, "temporal_scope": temporal_scope.value},
            )
        truncated = len(rows) > _MAX_SUPPORTING_MEMORIES
        rows = rows[:_MAX_SUPPORTING_MEMORIES]
        supporting: list[SupportingMemory] = []
        for row in rows:
            if row.memory is None:
                continue
            supporting.append(
                SupportingMemory(
                    memory_id=row.memory.id, content=row.memory.content, status=row.memory.status,
                    valid_from=row.memory.valid_from, valid_until=row.memory.valid_until,
                    created_at=row.memory.created_at,
                    superseded_by=row.links.get("superseded_by", []),
                    conflicts_with=row.links.get("conflicts_with", []),
                    sources=await self._sources(user_id, row.sources),
                )
            )  # fmt: skip
        rels = [r.relationship for r in rows]
        states = [TemporalState(r.state) for r in rows]
        statuses = [MemoryStatus(r["status"]) for r in rels]
        froms = [r.get("valid_from") for r in rels]
        untils = [r.get("valid_until") for r in rels]
        confidences = [r.get("confidence") for r in rels if r.get("confidence") is not None]
        state = (
            TemporalState.CURRENT if TemporalState.CURRENT in states
            else TemporalState.NOT_YET_VALID if TemporalState.NOT_YET_VALID in states
            else TemporalState.HISTORICAL
        )  # fmt: skip
        status = next(
            (st for st in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICTING) if st in statuses),
            statuses[0],
        )
        valid_from = None if any(f is None for f in froms) else min(froms)
        valid_until = None if any(u is None for u in untils) else max(untils)
        memory_count = len({m.memory_id for m in supporting})
        basis = [EvidenceBasis.DIRECTLY_STATED]
        if memory_count > 1:
            basis.append(EvidenceBasis.SUPPORTED_BY_MULTIPLE_MEMORIES)
        if temporal_scope is not TemporalScope.CURRENT and not (
            state is TemporalState.CURRENT and status in BELIEVED_STATUSES
        ):
            basis.append(EvidenceBasis.TEMPORALLY_DERIVED)
        return EdgeExplanation(
            source_entity_id=src.entity.id, target_entity_id=dst.entity.id, predicate=predicate,
            relation_kind=relation_kind(predicate), basis=basis, temporal_scope=temporal_scope,
            as_of=at, temporal_state=state, status=status, valid_from=valid_from,
            valid_until=valid_until, confidence=max(confidences) if confidences else None,
            assertion_count=len(rows),
            why=_why(state, status, valid_from, valid_until, memory_count, supporting),
            supporting_memories=supporting, truncated=truncated,
        )  # fmt: skip

    # -- entity context (M5) ----------------------------------------------------

    async def entity_context(
        self, *, user_id: str, entity_id: str, as_of: datetime | None = None
    ) -> EntityContext:
        s = self._settings
        at = resolve_scope(TemporalScope.ALL, as_of, self._now())
        resolved = await self._resolve(user_id, entity_id)
        entity = resolved.entity
        rel_cap = s.entity_context_max_relationships
        rows = await self._repo.entity_edges(
            user_id=user_id, entity_id=entity.id, scope=scope_params(TemporalScope.ALL, at),
            limit=rel_cap * 2 + 1,
        )  # fmt: skip
        truncated_rels = len(rows) > rel_cap * 2
        current: list[ContextRelationship] = []
        historical: list[ContextRelationship] = []
        for edge, other in rows[: rel_cap * 2]:
            view = ContextRelationship(
                other_entity_id=other["id"], other_name=other["name"],
                other_entity_type=other["entity_type"],
                direction="outgoing" if edge.source == entity.id else "incoming",
                predicate=edge.predicate, relation_kind=relation_kind(edge.predicate),
                status=MemoryStatus(edge.status), temporal_state=TemporalState(edge.state),
                valid_from=edge.valid_from, valid_until=edge.valid_until,
                assertion_count=edge.assertion_count, confidence=edge.confidence,
                memory_ids=edge.memory_ids,
            )  # fmt: skip
            if edge.state == TemporalState.CURRENT.value and _believed(edge.status):
                if len(current) < rel_cap:
                    current.append(view)
                else:
                    truncated_rels = True
            elif edge.state == TemporalState.HISTORICAL.value or not _believed(edge.status):
                if len(historical) < rel_cap:
                    historical.append(view)
                else:
                    truncated_rels = True
        related = _rank_related(current, historical)[: s.entity_context_max_related]
        mem_cap = s.entity_context_max_memories
        memories, total, conflicting = await self._repo.entity_memories(
            user_id=user_id, entity_id=entity.id, limit=mem_cap
        )
        ctx_memories = [
            ContextMemory(
                memory_id=m.id, content=m.content, memory_type=m.memory_type, status=m.status,
                temporal_state=temporal_state(m.valid_from, m.valid_until, at),
                valid_from=m.valid_from, valid_until=m.valid_until, created_at=m.created_at,
                source_types=types,
            )
            for m, types in memories
        ]  # fmt: skip
        timeline: list[TimelineEvent] = []
        for m, _ in memories:
            timeline.append(TimelineEvent(
                at=m.valid_from or m.created_at, kind="began", memory_id=m.id,
                content=m.content, status=m.status,
            ))  # fmt: skip
            if m.valid_until is not None:
                timeline.append(TimelineEvent(
                    at=m.valid_until, kind="ended", memory_id=m.id, content=m.content,
                    status=m.status,
                ))  # fmt: skip
        timeline.sort(key=lambda e: (e.at, e.kind, e.memory_id))
        merged = await self._repo.merged_from(user_id=user_id, entity_id=entity.id, limit=20)
        return EntityContext(
            entity=entity,
            resolved_from=resolved.original_id if resolved.was_redirected else None,
            merged_from=[MergedEntityRef(entity_id=i, name=n) for i, n in merged],
            as_of=at, current_relationships=current, historical_relationships=historical,
            related_entities=related, memories=ctx_memories, memory_total=total,
            conflicting_memory_total=conflicting, timeline=timeline,
            source_type_counts=dict(sorted(Counter(t for _, ts in memories for t in ts).items())),
            truncated_relationships=truncated_rels, truncated_memories=total > len(memories),
        )  # fmt: skip

    # -- memory evolution (M6) --------------------------------------------------

    async def memory_evolution(self, *, user_id: str, memory_id: str) -> MemoryEvolution:
        now = self._now()
        walk = await walk_memory_history(
            self._memory, user_id=user_id, memory_id=memory_id, max_chain=TIMELINE_MAX_CHAIN,
            follow_conflicts=True,
        )  # fmt: skip
        details = sorted(walk.details, key=_chronological)
        ids = [d.memory.id for d in details]
        events: list[EvolutionEvent] = []
        for d in details:
            m = d.memory
            srcs = await self._sources(user_id, d.sources)
            if d.duplicate_of:
                kind, related, detail = (
                    EvolutionKind.MERGE, d.duplicate_of,
                    "confirmed a duplicate of another memory and merged into it",
                )  # fmt: skip
            elif d.supersedes:
                kind, related, detail = (
                    EvolutionKind.SUPERSESSION, d.supersedes,
                    "replaced an earlier memory, which was kept as history",
                )  # fmt: skip
            elif m.status is MemoryStatus.CONFLICTING or d.conflicts_with:
                kind, related, detail = (
                    EvolutionKind.CONFLICT, d.conflicts_with,
                    "stated something that disagrees with another memory",
                )  # fmt: skip
            else:
                kind, related, detail = EvolutionKind.NEW_FACT, [], "first recorded"
            events.append(EvolutionEvent(
                kind=kind, at=m.valid_from or m.created_at, memory_id=m.id, content=m.content,
                status=m.status, related_memory_ids=sorted(related), detail=detail, sources=srcs,
            ))  # fmt: skip
            if m.status is MemoryStatus.INVALID:
                events.append(EvolutionEvent(
                    kind=EvolutionKind.INVALIDATION, at=m.updated_at, memory_id=m.id,
                    content=m.content, status=m.status, related_memory_ids=[],
                    detail=m.invalidation_reason or "retracted (no reason recorded)", sources=[],
                ))  # fmt: skip

        candidates = ExtractionCandidateRepository(self._session, user_id=user_id)
        reviews = MemoryReviewCandidateRepository(self._session, user_id=user_id)
        audit = ReviewAuditRepository(self._session, user_id=user_id)
        by_id = {d.memory.id: d for d in details}
        seen_reviews: set[str] = set()
        for mid in ids:
            for rec in await candidates.list_for_memory(memory_id=mid):
                if rec.decision in (DecisionType.DUPLICATE, DecisionType.UPDATE):
                    events.append(EvolutionEvent(
                        kind=EvolutionKind.RECONFIRMATION, at=rec.created_at, memory_id=mid,
                        content=by_id[mid].memory.content, status=by_id[mid].memory.status,
                        related_memory_ids=[],
                        detail=f"restated ({rec.decision.value.lower()}) from a later "
                        + ("message" if rec.source_message_id else "document"),
                        sources=[],
                    ))  # fmt: skip
            for review in await reviews.list_for_memory(memory_id=mid):
                if review["id"] in seen_reviews or review["status"] != "conflict_resolved":
                    continue
                seen_reviews.add(review["id"])
                rows = await audit.list_for_target(
                    target_type="memory_review_candidate", target_id=review["id"]
                )
                final = rows[-1] if rows else None
                resolution = (final or {}).get("resulting_state", {}).get("resolution", "resolved")
                events.append(EvolutionEvent(
                    kind=EvolutionKind.CONFLICT_RESOLVED,
                    at=(final or {}).get("created_at") or review["updated_at"], memory_id=mid,
                    content=by_id[mid].memory.content, status=by_id[mid].memory.status,
                    related_memory_ids=sorted(
                        {review["memory_id_a"], review["memory_id_b"]} - {mid}
                    ),
                    detail=f"a reviewer resolved the conflict: {resolution}", sources=[],
                ))  # fmt: skip
        events.sort(key=lambda e: (e.at, list(EvolutionKind).index(e.kind), e.memory_id))
        current = [
            d.memory.id for d in details
            if d.memory.status in BELIEVED_STATUSES
            and is_valid_at(d.memory.valid_from, d.memory.valid_until, now)
        ]  # fmt: skip
        truncated = walk.truncated or len(events) > _EVOLUTION_MAX_EVENTS
        return MemoryEvolution(
            memory_id=memory_id, events=events[:_EVOLUTION_MAX_EVENTS],
            current_memory_ids=current, truncated=truncated,
        )  # fmt: skip

    # -- context subgraph (M8, M9, M10) -----------------------------------------

    async def context_subgraph(
        self,
        *,
        user_id: str,
        query: str,
        temporal_scope: TemporalScope | None = None,
        as_of: datetime | None = None,
        max_nodes: int | None = None,
        max_evidence: int | None = None,
    ) -> ContextSubgraph:
        """The smallest useful graph slice for a question: evidence memories
        (hybrid retrieval), the entities they and the query name (anchors), the
        stored relationships among them that EVIDENCE asserts, and the shortest
        paths connecting the query's anchors. Neighbouring relationships no
        evidence asserts are dropped -- that is what keeps it small."""
        if self._hybrid is None:  # pragma: no cover - wiring error
            raise ValidationError("context subgraphs need hybrid retrieval")
        s = self._settings
        node_cap = clamp(max_nodes, s.graph_context_max_nodes)
        evidence_cap = clamp(max_evidence, s.graph_context_max_evidence)
        scope = temporal_scope or TemporalScope.CURRENT
        at = resolve_scope(scope, as_of, self._now())
        intent = _intent_for(temporal_scope, at)

        results = await self._hybrid.search(
            user_id=user_id, query=query, limit=evidence_cap, intent_override=intent
        )
        applied = (intent or detect_temporal_intent(query, now=at)).temporal.value

        cache: dict[str, ResolvedEntity | None] = {}

        async def resolve(entity_id: str) -> ResolvedEntity | None:
            if entity_id not in cache:  # one redirect lookup per entity per request
                cache[entity_id] = await self._retrieval.resolve_entity(
                    user_id=user_id, entity_id=entity_id
                )
            return cache[entity_id]

        anchors: list[Entity] = []
        for e in await self._hybrid.mentioned_entities(user_id=user_id, query=query):
            resolved = await resolve(e.id)
            if resolved and all(a.id != resolved.entity.id for a in anchors):
                anchors.append(resolved.entity)
        anchor_ids = [a.id for a in anchors][:4]

        entities: dict[str, Entity] = {a.id: a for a in anchors if a.id in anchor_ids}
        mentioned_by: dict[str, set[str]] = {}
        # Only CONTENT-matched evidence (vector / keyword channel) contributes
        # its entities: a memory found solely because it mentions an anchor is
        # a neighbour, not an answer -- letting it in is exactly the
        # irrelevant-neighbour noise a context subgraph exists to remove. It
        # stays in `evidence` (nothing is hidden). Fallback when nothing
        # matched on content: the top three.
        # A vector-only match must also be reasonably similar: a weak one mostly
        # reflects sharing the anchor's own name (heuristic threshold,
        # `graph_context_min_vector_similarity`; calibrated on the fake
        # embeddings -- recalibrate for a real embedding model).
        min_sim = s.graph_context_min_vector_similarity

        def content_matched(r) -> bool:  # noqa: ANN001
            ex = r.explanation
            if "keyword" in ex.retrieved_via:
                return True
            return "vector" in ex.retrieved_via and (ex.vector_similarity or 0.0) >= min_sim

        contributing = [r for r in results if content_matched(r)] or results[:3]
        for r in contributing:
            for e in r.memory.entities:
                resolved = await resolve(e.id)
                if resolved is None:
                    continue
                entities.setdefault(resolved.entity.id, resolved.entity)
                mentioned_by.setdefault(resolved.entity.id, set()).add(r.memory.memory.id)

        # Paths between the query's anchors connect what the question names.
        paths: list[GraphPath] = []
        path_entities: dict[str, Entity] = {}
        for i, a in enumerate(anchor_ids):
            for b in anchor_ids[i + 1 :]:
                found = await self.paths(
                    user_id=user_id, source_entity_id=a, target_entity_id=b, max_hops=3,
                    max_paths=2, temporal_scope=scope, as_of=as_of,
                )  # fmt: skip
                paths.extend(found.paths[:1])
                path_entities.update(self.last_path_entities)
        on_path_entities = {eid for p in paths for eid in p.entity_ids}
        on_path_pairs = {
            frozenset((p.entity_ids[i], p.entity_ids[i + 1]))
            for p in paths for i in range(len(p.entity_ids) - 1)
        }  # fmt: skip
        for p in paths:
            for eid in p.entity_ids:
                if eid not in entities and eid in path_entities:
                    entities[eid] = path_entities[eid]

        candidate_ids = list(entities)
        rows = await self._repo.slice_edges(
            user_id=user_id, slice_ids=candidate_ids, reach_ids=candidate_ids,
            scope=scope_params(scope, at), predicates=[],
            limit=s.graph_explore_max_edges + 1,
        )  # fmt: skip
        evidence_set = {r.memory.memory.id for r in contributing}
        kept_edges: list[GraphSliceEdge] = []
        edge_signals: dict[str, dict[str, float]] = {}
        for r in rows[: s.graph_explore_max_edges]:
            from_evidence = 1.0 if evidence_set & set(r.memory_ids) else 0.0
            on_path = 1.0 if frozenset((r.source, r.target)) in on_path_pairs else 0.0
            if not (from_evidence or on_path):
                continue  # a neighbour relationship nothing in the evidence asserts
            touches = 1.0 if {r.source, r.target} & set(anchor_ids) else 0.0
            current = 1.0 if r.state == TemporalState.CURRENT.value else 0.0
            support = min(r.assertion_count, 3) / 3
            score = round(
                0.35 * from_evidence + 0.25 * on_path + 0.15 * touches + 0.15 * current
                + 0.1 * support, 4,
            )  # fmt: skip
            edge_id = relates_to_edge_id(r.source, r.predicate, r.target, r.status, r.state)
            edge_signals[edge_id] = {
                "asserted_by_evidence": from_evidence, "on_anchor_path": on_path,
                "touches_anchor": touches, "current": current, "support": round(support, 3),
                "score": score,
            }  # fmt: skip
            kept_edges.append(GraphSliceEdge(
                id=edge_id, kind=EdgeKind.RELATES_TO, source=r.source, target=r.target,
                predicate=r.predicate, status=r.status, valid_from=r.valid_from,
                valid_until=r.valid_until, temporal_state=TemporalState(r.state),
                memory_ids=r.memory_ids, assertion_count=r.assertion_count,
            ))  # fmt: skip

        incident = {e.source for e in kept_edges} | {e.target for e in kept_edges}
        keep = [
            eid for eid in candidate_ids
            if eid in anchor_ids or eid in incident or eid in mentioned_by
        ]  # fmt: skip
        node_signals: dict[str, dict[str, float]] = {}
        for eid in keep:
            anchor = 1.0 if eid in anchor_ids else 0.0
            evidence = min(len(mentioned_by.get(eid, ())), 3) / 3
            on_path = 1.0 if eid in on_path_entities else 0.0
            degree = min(sum(1 for e in kept_edges if eid in (e.source, e.target)), 3) / 3
            node_signals[eid] = {
                "anchor": anchor, "mentioned_by_evidence": round(evidence, 3),
                "on_anchor_path": on_path, "kept_degree": round(degree, 3),
                "score": round(0.4 * anchor + 0.3 * evidence + 0.2 * on_path + 0.1 * degree, 4),
            }  # fmt: skip
        keep.sort(key=lambda i: (-node_signals[i]["score"], entities[i].name or "", i))
        truncated = len(keep) > node_cap
        keep = keep[:node_cap]
        kept = set(keep)
        kept_edges = [e for e in kept_edges if e.source in kept and e.target in kept]

        nodes = [
            GraphSliceNode(
                id=eid, kind=NodeKind.ENTITY, label=entities[eid].name or eid,
                entity_type=entities[eid].entity_type,
            )
            for eid in keep
        ]  # fmt: skip
        evidence: list[EvidenceItem] = []
        for r in results:
            m, ex = r.memory.memory, r.explanation
            nodes.append(GraphSliceNode(
                id=m.id, kind=NodeKind.MEMORY, label=m.content[:200], memory_type=m.memory_type,
                status=m.status, confidence=m.confidence, valid_from=m.valid_from,
                valid_until=m.valid_until,
                temporal_state=temporal_state(m.valid_from, m.valid_until, at),
                created_at=m.created_at,
            ))  # fmt: skip
            for eid, mids in mentioned_by.items():
                if m.id in mids and eid in kept:
                    kept_edges.append(GraphSliceEdge(
                        id=f"mentions:{m.id}:{eid}", kind=EdgeKind.MENTIONS,
                        source=m.id, target=eid,
                    ))  # fmt: skip
            evidence.append(EvidenceItem(
                memory_id=m.id, content=m.content, status=m.status,
                temporal_state=ex.temporal_state, retrieved_via=list(ex.retrieved_via),
                signals={
                    k: float(v) for k, v in {
                        "vector_similarity": ex.vector_similarity,
                        "graph_relevance": ex.graph_relevance,
                        "keyword_match": ex.keyword_match,
                        "temporal_fit": ex.temporal_fit,
                        "confidence": ex.confidence,
                    }.items() if v is not None
                },
                score=round(float(ex.final_score), 4),
                sources=await self._sources(user_id, r.memory.sources),
            ))  # fmt: skip
        return ContextSubgraph(
            temporal_scope=temporal_scope, temporal_intent=applied, as_of=at,
            anchors=anchor_ids, nodes=nodes, edges=sorted(kept_edges, key=lambda e: e.id),
            node_signals=node_signals, edge_signals=edge_signals, evidence=evidence, paths=paths,
            construction={
                "evidence_memories": len(results),
            "contributing_evidence": len(contributing),
            "anchor_entities": len(anchor_ids),
                "candidate_entities": len(candidate_ids), "candidate_relationships": len(rows),
                "kept_entities": len(keep), "kept_relationships":
                sum(1 for e in kept_edges if e.kind is EdgeKind.RELATES_TO),
                "anchor_paths": len(paths),
            },
            max_nodes=node_cap, truncated=truncated or len(rows) > s.graph_explore_max_edges,
        )  # fmt: skip


def _intent_for(scope: TemporalScope | None, at: datetime) -> QueryIntent | None:
    if scope is None:
        return None  # retrieval detects intent from the query text
    return {
        TemporalScope.CURRENT: QueryIntent(TemporalIntent.CURRENT),
        TemporalScope.HISTORICAL: QueryIntent(TemporalIntent.HISTORICAL),
        TemporalScope.AS_OF: QueryIntent(TemporalIntent.AS_OF, as_of=at),
        TemporalScope.ALL: QueryIntent(TemporalIntent.UNSPECIFIED),
    }[scope]


def _chronological(detail: MemoryDetail) -> tuple:
    m = detail.memory
    return (m.valid_from is not None, m.valid_from or m.created_at, m.created_at, m.id)


def _rank_related(
    current: list[ContextRelationship], historical: list[ContextRelationship]
) -> list[RelatedEntity]:
    """Neighbours ranked by explainable signals: currently related, how many
    memories support it, and in how many distinct ways they relate."""
    by: dict[str, dict] = {}
    for rel, is_current in [(r, True) for r in current] + [(r, False) for r in historical]:
        b = by.setdefault(
            rel.other_entity_id,
            {"name": rel.other_name, "type": rel.other_entity_type, "preds": set(),
             "current": False, "support": 0},
        )  # fmt: skip
        b["preds"].add(rel.predicate)
        b["current"] = b["current"] or is_current
        b["support"] += rel.assertion_count
    ranked = []
    for eid, b in by.items():
        signals = {
            "current": 1.0 if b["current"] else 0.0,
            "support": round(min(b["support"], 5) / 5, 3),
            "relation_variety": round(min(len(b["preds"]), 3) / 3, 3),
        }
        score = round(0.5 * signals["current"] + 0.35 * signals["support"]
                      + 0.15 * signals["relation_variety"], 4)  # fmt: skip
        ranked.append(RelatedEntity(
            entity_id=eid, name=b["name"], entity_type=b["type"], predicates=sorted(b["preds"]),
            signals=signals, score=score,
        ))  # fmt: skip
    ranked.sort(key=lambda r: (-r.score, r.name, r.entity_id))
    return ranked


def _fmt(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else "an unrecorded time"


def _why(
    state: TemporalState,
    status: MemoryStatus,
    valid_from: datetime | None,
    valid_until: datetime | None,
    memory_count: int,
    supporting: list[SupportingMemory],
) -> str:
    """One sentence, built only from stored windows, statuses and links."""
    stated = f"stated by {memory_count} memor{'y' if memory_count == 1 else 'ies'}"
    replaced = sorted({x for m in supporting for x in m.superseded_by})
    disputed = sorted({x for m in supporting for x in m.conflicts_with})
    if state is TemporalState.CURRENT and status in BELIEVED_STATUSES:
        end = f" until {_fmt(valid_until)}" if valid_until else "; no end recorded"
        text = f"Current: {stated}, valid from {_fmt(valid_from)}{end}."
        if status is MemoryStatus.CONFLICTING or disputed:
            n = len(disputed) or 1
            text += f" Disputed by {n} other memor{'y' if n == 1 else 'ies'}."
        return text
    if state is TemporalState.NOT_YET_VALID:
        return f"Not yet true: {stated}, valid from {_fmt(valid_from)}."
    reason = (
        f"; replaced by {len(replaced)} later memor{'y' if len(replaced) == 1 else 'ies'}"
        if replaced
        else ""
    )
    return f"No longer true: {stated}, ended {_fmt(valid_until)} (status {status.value}){reason}."
