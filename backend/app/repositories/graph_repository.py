"""Bounded, read-only graph projections for the memory graph exploration API
(Phase 8, docs/PHASE8_PLAN.md sections 7.4, 9, 13, 14).

Every statement here is a read. Every node pattern and every relationship
predicate filters on `$user_id` (the same discipline as `entity_repository`),
and every statement is bounded by a `LIMIT` (or by a caller-bounded `UNWIND`
list). No value is ever formatted into query text: unlike the existing
`NEIGHBOURHOOD_*` templates, traversal here is a per-hop breadth-first
expansion driven from Python, so there is no variable-length hop bound to
interpolate at all. This module is covered by `tests/unit/test_cypher_safety.py`
like every other repository module containing Cypher.

Temporal and status scope is applied INSIDE the Cypher, on every hop, never
after a LIMIT -- filtering after the fact would under-fill a budget and
misrepresent the slice. The scope predicate is one parameterised form for all
three scopes (`current` / `as_of` / `all`), selected by boolean parameters
rather than by building different query text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.graph.connection import GraphClient
from app.models.domain import Entity, Memory, Source
from app.models.enums import BELIEVED_STATUSES, EXCLUDED_FROM_DEFAULT_RETRIEVAL
from app.repositories.mapping import to_entity, to_memory, to_source
from app.schemas.graph import TemporalScope

EXCLUDED_STATUSES: list[str] = sorted(s.value for s in EXCLUDED_FROM_DEFAULT_RETRIEVAL)
BELIEVED: list[str] = sorted(s.value for s in BELIEVED_STATUSES)

#: Max memory ids carried on one aggregated `relates_to` slice edge. The full
#: list is the evidence endpoint's job; this only keeps one edge from carrying
#: an unbounded list through a hub.
RELATES_TO_MEMORY_IDS_CAP = 20

# -- Reusable predicate text (joined into whole statements at import time; the
#    audited constants below are complete, fully-parameterised statements) ----


def _utc(expr: str) -> str:
    """`expr` as a UTC-offset DateTime. Neo4j orders two DateTimes at the SAME
    instant by zone representation too (`datetime('...[UTC]') <= datetime('...Z')`
    is false), and a value read back from Neo4j and re-written -- e.g. a
    merge-copied RELATES_TO window -- carries a named `UTC` zone while a Python
    `datetime` parameter carries an offset. Normalising both sides makes every
    window comparison below instant-based, i.e. exactly `lifecycle.is_valid_at`.
    """
    return f"datetime({{datetime: {expr}, timezone: '+00:00'}})"


_AT = _utc("$at")


def _window_contains_at(var: str) -> str:
    return (
        f"(({var}.valid_from IS NULL OR {_utc(var + '.valid_from')} <= {_AT})\n"
        f"      AND ({var}.valid_until IS NULL OR {_utc(var + '.valid_until')} > {_AT}))"
    )


#: A RELATES_TO edge `r` is in scope. Half-open window `[valid_from,
#: valid_until)`, identical in meaning to memory_repository's `as_of` fragment.
#: Split in two so the cheap property checks run before the per-edge index
#: lookup (`_ASSERTING_MEMORY_IN_SCOPE`) -- measured at 10k entities, running
#: the lookup first on every expanded hub edge dominated the query cost.
_EDGE_PROPERTIES_IN_SCOPE = f"""r.user_id = $user_id
  AND NOT r.status IN $excluded_statuses
  AND ($any_status OR r.status IN $believed_statuses)
  AND ($ignore_window OR {_window_contains_at("r")})"""

#: An edge whose asserting memory is excluded from default retrieval (e.g. a
#: MERGED duplicate, whose own edge keeps status `active` -- ADR 0010) is out of
#: scope too, so a slice never shows a fact only a merged duplicate asserts.
_ASSERTING_MEMORY_IN_SCOPE = """NOT EXISTS {
      MATCH (asserting:Memory {id: r.memory_id, user_id: $user_id})
      WHERE asserting.status IN $excluded_statuses
  }"""

_EDGE_IN_SCOPE = f"""{_EDGE_PROPERTIES_IN_SCOPE}
  AND {_ASSERTING_MEMORY_IN_SCOPE}"""

_PREDICATE_FILTER = "(size($predicates) = 0 OR r.predicate IN $predicates)"

#: Same temporal/status scope, for a memory node `m`.
_MEMORY_IN_SCOPE = f"""NOT m.status IN $excluded_statuses
  AND ($any_status OR m.status IN $believed_statuses)
  AND ($ignore_window OR {_window_contains_at("m")})"""

#: Server-computed temporal state of an edge `r` relative to `$at`. Mirrors
#: `app.schemas.graph.temporal_state` (pinned by an integration test).
_EDGE_TEMPORAL_STATE = f"""CASE
    WHEN r.valid_from IS NOT NULL AND {_utc("r.valid_from")} > {_AT} THEN 'not_yet_valid'
    WHEN r.valid_until IS NOT NULL AND {_utc("r.valid_until")} <= {_AT} THEN 'historical'
    ELSE 'current'
  END"""

#: A merged-away entity (it has an outgoing MERGED_INTO) never appears in a
#: slice -- the same test `RESOLVE_ENTITY` uses to recognise a canonical entity.
_NOT_MERGED_AWAY = "NOT (b)-[:MERGED_INTO]->()"

_ENTITY_TYPE_FILTER = "(size($entity_types) = 0 OR b.entity_type IN $entity_types)"

#: One breadth-first hop: the distinct, in-scope, not-yet-seen neighbours of
#: the frontier. Seen ids (root, earlier hops, client `exclude_ids`) are
#: neither returned nor re-expanded. `$limit` is the remaining node budget + 1,
#: which is what makes `truncated_nodes` exact. The `WITH DISTINCT` barrier
#: keeps the asserting-memory lookup behind every cheaper filter.
EXPAND_FRONTIER = f"""
UNWIND $frontier AS fid
MATCH (a:Entity {{id: fid, user_id: $user_id}})-[r:RELATES_TO]-(b:Entity {{user_id: $user_id}})
WHERE NOT b.id IN $seen
  AND {_EDGE_PROPERTIES_IN_SCOPE}
  AND {_PREDICATE_FILTER}
  AND {_NOT_MERGED_AWAY}
  AND {_ENTITY_TYPE_FILTER}
WITH DISTINCT r, b
WHERE {_ASSERTING_MEMORY_IN_SCOPE}
WITH DISTINCT b
RETURN b {{.*}} AS entity
ORDER BY b.name, b.id
LIMIT $limit
"""

#: In-scope RELATES_TO edges with at least one endpoint in `$slice_ids` and
#: the other in `$reach_ids` (slice + client-loaded `exclude_ids`, so a new
#: node's edges back into already-loaded nodes are returned), aggregated per
#: `(source, predicate, target, status, temporal_state)`. `$limit` is the edge
#: budget + 1 (exact `truncated_edges`).
SLICE_EDGES = f"""
UNWIND $slice_ids AS sid
MATCH (a:Entity {{id: sid, user_id: $user_id}})-[r:RELATES_TO]-(b:Entity {{user_id: $user_id}})
WHERE b.id IN $reach_ids
  AND {_EDGE_PROPERTIES_IN_SCOPE}
  AND {_PREDICATE_FILTER}
  AND {_NOT_MERGED_AWAY}
WITH DISTINCT r
WHERE {_ASSERTING_MEMORY_IN_SCOPE}
WITH r, startNode(r).id AS source, endNode(r).id AS target, {_EDGE_TEMPORAL_STATE} AS state
ORDER BY r.created_at, r.id
WITH source, target, r.predicate AS predicate, r.status AS status, state,
     collect(DISTINCT r.memory_id) AS memory_ids,
     count(r) AS assertion_count,
     CASE WHEN count(r.valid_from) < count(r) THEN null ELSE min(r.valid_from) END
         AS valid_from,
     CASE WHEN count(r.valid_until) < count(r) THEN null ELSE max(r.valid_until) END
         AS valid_until
RETURN source, target, predicate, status, state,
       memory_ids[0..$memory_ids_cap] AS memory_ids,
       assertion_count, valid_from, valid_until
ORDER BY source, predicate, target, status, state
LIMIT $limit
"""

#: Landing slice: the user's canonical (not merged-away) entities, most
#: recently updated first (ties by name, then id -- deterministic).
#: `$limit` is the budget + 1. This is PHASE8_PLAN.md section 9.1's approved
#: FALLBACK ordering: ranking by in-scope degree was measured at ~3.8 s warm
#: (5.5M db hits) at 10k synthetic entities, vs ~62 ms for this ordering --
#: see docs/PHASE8_PLAN.md section 25. The edges among the returned entities
#: are still in-scope-filtered exactly as everywhere else.
OVERVIEW_ENTITIES = f"""
MATCH (b:Entity {{user_id: $user_id}})
WHERE {_NOT_MERGED_AWAY}
  AND {_ENTITY_TYPE_FILTER}
RETURN b {{.*}} AS entity
ORDER BY b.updated_at DESC, b.name, b.id
LIMIT $limit
"""

#: Memory nodes mentioning each slice entity, newest first, in scope, at most
#: `$per_entity` (+1 so per-entity truncation is exact) per entity.
SLICE_MEMORIES = f"""
UNWIND $entity_ids AS eid
MATCH (m:Memory {{user_id: $user_id}})-[:MENTIONS]->(:Entity {{id: eid, user_id: $user_id}})
WHERE {_MEMORY_IN_SCOPE}
WITH eid, m
ORDER BY m.created_at DESC, m.id
WITH eid, collect(m {{.*}})[0..$per_entity] AS memories
RETURN eid AS entity_id, memories
"""

#: SUPERSEDES / CONFLICTS_WITH / DUPLICATE_OF among a bounded set of memories.
MEMORY_LINKS = """
UNWIND $memory_ids AS mid
MATCH (a:Memory {id: mid, user_id: $user_id})-[l:SUPERSEDES|CONFLICTS_WITH|DUPLICATE_OF]->
      (b:Memory {user_id: $user_id})
WHERE b.id IN $memory_ids
RETURN DISTINCT type(l) AS kind, a.id AS source, b.id AS target
ORDER BY kind, source, target
LIMIT $limit
"""

#: `merged_from_count` for a bounded set of entities: direct MERGED_INTO in-degree.
MERGED_FROM_COUNTS = """
UNWIND $entity_ids AS eid
MATCH (e:Entity {id: eid, user_id: $user_id})
RETURN eid AS entity_id,
       COUNT { (:Entity {user_id: $user_id})-[:MERGED_INTO]->(e) } AS merged_from_count
"""

#: The stored RELATES_TO edges for one directed `(source, predicate, target)`,
#: each with its asserting memory, that memory's link fields and its sources --
#: one round trip, anchored on two uniquely-constrained Entity.id lookups (no
#: RELATES_TO.id lookup, so no new index). `$limit` is the evidence limit + 1.
EDGE_EVIDENCE = f"""
MATCH (a:Entity {{id: $source_entity_id, user_id: $user_id}})
      -[r:RELATES_TO {{predicate: $predicate}}]->
      (b:Entity {{id: $target_entity_id, user_id: $user_id}})
WHERE {_EDGE_IN_SCOPE}
WITH r, {_EDGE_TEMPORAL_STATE} AS state
ORDER BY r.created_at, r.id
LIMIT $limit
OPTIONAL MATCH (m:Memory {{id: r.memory_id, user_id: $user_id}})
RETURN r {{.*}} AS relationship, state,
       m {{.*}} AS memory,
       COLLECT {{ MATCH (m)-[:DERIVED_FROM]->(s:Source {{user_id: $user_id}})
                 RETURN s {{.*}} ORDER BY s.created_at, s.id }} AS sources,
       COLLECT {{ MATCH (m)-[:SUPERSEDES]->(x:Memory {{user_id: $user_id}})
                 RETURN x.id ORDER BY x.id }} AS supersedes,
       COLLECT {{ MATCH (x:Memory {{user_id: $user_id}})-[:SUPERSEDES]->(m)
                 RETURN x.id ORDER BY x.id }} AS superseded_by,
       COLLECT {{ MATCH (m)-[:CONFLICTS_WITH]-(x:Memory {{user_id: $user_id}})
                 RETURN DISTINCT x.id AS id ORDER BY id }} AS conflicts_with,
       COLLECT {{ MATCH (m)-[:DUPLICATE_OF]->(x:Memory {{user_id: $user_id}})
                 RETURN x.id ORDER BY x.id }} AS duplicate_of,
       COLLECT {{ MATCH (x:Memory {{user_id: $user_id}})-[:DUPLICATE_OF]->(m)
                 RETURN x.id ORDER BY x.id }} AS duplicates
ORDER BY r.created_at, r.id
"""

USER_SCOPED_STATEMENTS: tuple[str, ...] = (
    EXPAND_FRONTIER,
    SLICE_EDGES,
    OVERVIEW_ENTITIES,
    SLICE_MEMORIES,
    MEMORY_LINKS,
    MERGED_FROM_COUNTS,
    EDGE_EVIDENCE,
)


def scope_params(scope: TemporalScope, at: datetime) -> dict[str, Any]:
    """The parameters that select one temporal scope in the shared predicate."""
    return {
        "excluded_statuses": EXCLUDED_STATUSES,
        "believed_statuses": BELIEVED,
        "any_status": scope is not TemporalScope.CURRENT,
        "ignore_window": scope is TemporalScope.ALL,
        "at": at,
    }


@dataclass(frozen=True)
class SliceEdgeRow:
    source: str
    target: str
    predicate: str
    status: str
    state: str
    memory_ids: list[str]
    assertion_count: int
    valid_from: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True)
class EvidenceRow:
    relationship: dict[str, Any]
    state: str
    memory: Memory | None
    sources: list[Source] = field(default_factory=list)
    links: dict[str, list[str]] = field(default_factory=dict)


class GraphRepository:
    """Bounded graph projections. Stateless beyond the client."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def expand_frontier(
        self,
        *,
        user_id: str,
        frontier: list[str],
        seen: list[str],
        scope: dict[str, Any],
        predicates: list[str],
        entity_types: list[str],
        limit: int,
    ) -> list[Entity]:
        if not frontier or limit <= 0:
            return []
        rows = await self._client.read(
            EXPAND_FRONTIER,
            {
                **scope,
                "user_id": user_id,
                "frontier": frontier,
                "seen": seen,
                "predicates": predicates,
                "entity_types": entity_types,
                "limit": limit,
            },
        )
        return [to_entity(r["entity"]) for r in rows]

    async def slice_edges(
        self,
        *,
        user_id: str,
        slice_ids: list[str],
        reach_ids: list[str],
        scope: dict[str, Any],
        predicates: list[str],
        limit: int,
    ) -> list[SliceEdgeRow]:
        if not slice_ids:
            return []
        rows = await self._client.read(
            SLICE_EDGES,
            {
                **scope,
                "user_id": user_id,
                "slice_ids": slice_ids,
                "reach_ids": reach_ids,
                "predicates": predicates,
                "memory_ids_cap": RELATES_TO_MEMORY_IDS_CAP,
                "limit": limit,
            },
        )
        return [
            SliceEdgeRow(
                source=r["source"],
                target=r["target"],
                predicate=r["predicate"],
                status=r["status"],
                state=r["state"],
                memory_ids=list(r["memory_ids"]),
                assertion_count=int(r["assertion_count"]),
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
            )
            for r in rows
        ]

    async def overview_entities(
        self,
        *,
        user_id: str,
        scope: dict[str, Any],
        entity_types: list[str],
        limit: int,
    ) -> list[Entity]:
        rows = await self._client.read(
            OVERVIEW_ENTITIES,
            {"user_id": user_id, "entity_types": entity_types, "limit": limit},
        )
        return [to_entity(r["entity"]) for r in rows]

    async def slice_memories(
        self,
        *,
        user_id: str,
        entity_ids: list[str],
        scope: dict[str, Any],
        per_entity: int,
    ) -> dict[str, list[Memory]]:
        """`{entity_id: [memory, ...]}`, each list at most `per_entity` long,
        in the input order of `entity_ids`; entities with no in-scope memory
        are absent."""
        if not entity_ids:
            return {}
        rows = await self._client.read(
            SLICE_MEMORIES,
            {**scope, "user_id": user_id, "entity_ids": entity_ids, "per_entity": per_entity},
        )
        return {
            r["entity_id"]: [to_memory(m) for m in r["memories"]]
            for r in rows
            if r["memories"]
        }

    async def memory_links(
        self, *, user_id: str, memory_ids: list[str], limit: int
    ) -> list[tuple[str, str, str]]:
        if not memory_ids or limit <= 0:
            return []
        rows = await self._client.read(
            MEMORY_LINKS, {"user_id": user_id, "memory_ids": memory_ids, "limit": limit}
        )
        return [(r["kind"], r["source"], r["target"]) for r in rows]

    async def merged_from_counts(
        self, *, user_id: str, entity_ids: list[str]
    ) -> dict[str, int]:
        if not entity_ids:
            return {}
        rows = await self._client.read(
            MERGED_FROM_COUNTS, {"user_id": user_id, "entity_ids": entity_ids}
        )
        return {r["entity_id"]: int(r["merged_from_count"]) for r in rows}

    async def edge_evidence(
        self,
        *,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        predicate: str,
        scope: dict[str, Any],
        limit: int,
    ) -> list[EvidenceRow]:
        rows = await self._client.read(
            EDGE_EVIDENCE,
            {
                **scope,
                "user_id": user_id,
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "predicate": predicate,
                "limit": limit,
            },
        )
        results: list[EvidenceRow] = []
        for r in rows:
            memory = to_memory(r["memory"]) if r["memory"] is not None else None
            results.append(
                EvidenceRow(
                    relationship=r["relationship"],
                    state=r["state"],
                    memory=memory,
                    sources=[to_source(s) for s in r["sources"]] if memory else [],
                    links={
                        key: list(r[key])
                        for key in (
                            "supersedes",
                            "superseded_by",
                            "conflicts_with",
                            "duplicate_of",
                            "duplicates",
                        )
                    }
                    if memory
                    else {},
                )
            )
        return results
