"""Entity and graph-traversal persistence.

As in `memory_repository`, every statement filters on `$user_id`, and the traversal
statements additionally require *every relationship along the path* to belong to the
requesting user -- otherwise a multi-hop query could walk out of the user's own
subgraph via a shared node.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.graph.connection import GraphClient, TxStep
from app.models.domain import (
    Entity,
    EntityDetail,
    GraphNode,
    GraphNodeWithPath,
    GraphPathStep,
    Memory,
    Neighborhood,
    Relationship,
    RelationshipView,
    ResolvedEntity,
)
from app.models.enums import Direction
from app.repositories.mapping import to_entity, to_memory, to_relationship

#: Bound on how many MERGED_INTO hops the resolver will follow. Not a business
#: tunable -- a safety bound (ADR 0010, PHASE4_PLAN.md section 9): redirect
#: chains longer than this are not expected in practice, and Cypher's own
#: per-path relationship uniqueness (no edge reused twice in one path) already
#: makes a cycle terminate within this bound rather than loop -- see
#: `resolve_entity`'s docstring for why no separate visited-set is needed.
_MAX_REDIRECT_DEPTH = 5

GET_ENTITY = """
MATCH (e:Entity {id: $entity_id, user_id: $user_id})
OPTIONAL MATCH (m:Memory {user_id: $user_id})-[:MENTIONS]->(e)
OPTIONAL MATCH (e)-[r:RELATES_TO {user_id: $user_id}]-(:Entity {user_id: $user_id})
RETURN e {.*} AS entity,
       count(DISTINCT m) AS memory_count,
       count(DISTINCT r) AS relationship_count
"""

FIND_ENTITY_BY_NAME = """
MATCH (e:Entity {user_id: $user_id, normalized_name: $normalized_name})
RETURN e {.*} AS entity
"""

#: Phase 8 (D2): a merged-away entity (outgoing MERGED_INTO) is hidden unless
#: `$include_merged` -- its name lives on as an alias of the canonical entity,
#: so searching for it still finds the canonical one, and its id still
#: resolves through `RESOLVE_ENTITY`.
SEARCH_ENTITIES = """
MATCH (e:Entity {user_id: $user_id})
WHERE (toLower(e.name) CONTAINS toLower($query)
       OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS toLower($query)))
  AND ($include_merged OR NOT (e)-[:MERGED_INTO]->())
RETURN e {.*} AS entity
ORDER BY e.name
SKIP $offset LIMIT $limit
"""

#: Phase 2: every entity for a user, unfiltered and bounded. Used by ambiguity
#: detection (app/extraction/resolution.py), which must compare a candidate name
#: against every existing entity -- a substring CONTAINS search cannot find
#: "OpenAI" starting from a query of "Open AI" (the space breaks containment in
#: both directions), which is exactly the punctuation/whitespace case ambiguity
#: detection exists to catch. Not exposed through the public search endpoint.
LIST_ALL_ENTITIES = """
MATCH (e:Entity {user_id: $user_id})
RETURN e {.*} AS entity
ORDER BY e.name
LIMIT $limit
"""

#: Relationships incident to an entity, with both endpoints. The direction filter is
#: applied in Python against the returned endpoint ids rather than by swapping the
#: pattern, which keeps one statement instead of three near-identical ones.
GET_RELATIONSHIPS = """
MATCH (a:Entity {user_id: $user_id})-[r:RELATES_TO {user_id: $user_id}]->
      (b:Entity {user_id: $user_id})
WHERE (a.id = $entity_id OR b.id = $entity_id)
  AND NOT r.status IN $excluded_statuses
RETURN r {.*, source_entity_id: a.id, target_entity_id: b.id} AS relationship,
       a {.*} AS source_entity,
       b {.*} AS target_entity
ORDER BY r.created_at DESC, r.id
"""

GET_ENTITY_MEMORIES = """
MATCH (m:Memory {user_id: $user_id})-[:MENTIONS]->(:Entity {id: $entity_id,
      user_id: $user_id})
WHERE NOT m.status IN $excluded_statuses
RETURN m {.*} AS memory
ORDER BY m.created_at DESC, m.id
SKIP $offset LIMIT $limit
"""

#: Batched counterpart to `GET_ENTITY_MEMORIES` for a whole set of entities in
#: ONE round trip (PHASE4_PLAN.md section 14): the dense-graph benchmark
#: measured hybrid retrieval's graph channel doing one `GET_ENTITY_MEMORIES`
#: call per neighbourhood node -- up to `max_nodes` sequential round trips --
#: and that N+1 pattern, not the traversal itself, was the actual scaling
#: bottleneck (39-105s at 10k nodes/200 nodes-returned vs. <1s of that being
#: the traversal). `[0..$limit_per_entity]` slices AFTER the per-group
#: `ORDER BY`, which Cypher preserves into `collect()` -- same technique as
#: `NEIGHBOURHOOD_PATHS_TEMPLATE`'s shortest-path selection.
GET_MEMORIES_FOR_ENTITIES = """
UNWIND $entity_ids AS eid
MATCH (m:Memory {user_id: $user_id})-[:MENTIONS]->(:Entity {id: eid, user_id: $user_id})
WHERE NOT m.status IN $excluded_statuses
WITH eid, m
ORDER BY m.created_at DESC, m.id
WITH eid, collect(m {.*})[0..$limit_per_entity] AS memories
RETURN eid AS entity_id, memories
"""

#: N-hop node discovery. The hop bound cannot be a query parameter in Cypher, so it is
#: interpolated -- but only after `validate_hops` has proven it is an int within the
#: configured range, so no caller text ever reaches the query.
NEIGHBOURHOOD_NODES_TEMPLATE = """
MATCH (root:Entity {{id: $entity_id, user_id: $user_id}})
MATCH path = (root)-[rels:RELATES_TO*1..{hops}]-(other:Entity {{user_id: $user_id}})
WHERE all(r IN rels WHERE r.user_id = $user_id AND NOT r.status IN $excluded_statuses)
  AND all(n IN nodes(path) WHERE n.user_id = $user_id)
  AND other.id <> $entity_id
WITH other, min(length(path)) AS hops
RETURN other {{.*}} AS entity, hops
ORDER BY hops, other.name, other.id
LIMIT $max_nodes
"""

NEIGHBOURHOOD_EDGES = """
MATCH (a:Entity {user_id: $user_id})-[r:RELATES_TO {user_id: $user_id}]->
      (b:Entity {user_id: $user_id})
WHERE a.id IN $ids AND b.id IN $ids
  AND NOT r.status IN $excluded_statuses
WITH DISTINCT r, a, b
RETURN r {.*, source_entity_id: a.id, target_entity_id: b.id} AS relationship
ORDER BY r.id
LIMIT $max_edges
"""

#: PHASE3_PLAN.md's own intended refinement, built in Phase 4: the same node
#: discovery as NEIGHBOURHOOD_NODES_TEMPLATE, but additionally capturing ONE
#: shortest path per node (ORDER BY length before collect(path)[0] preserves
#: order into the aggregation) so retrieval can report *how* a memory was
#: reached, not just how far. Kept as a separate template from the existing
#: one -- the interactive GET /entities/{id}/neighbourhood endpoint has no
#: consumer for per-node paths and shouldn't pay the extra `collect(path)` cost.
NEIGHBOURHOOD_PATHS_TEMPLATE = """
MATCH (root:Entity {{id: $entity_id, user_id: $user_id}})
MATCH path = (root)-[rels:RELATES_TO*1..{hops}]-(other:Entity {{user_id: $user_id}})
WHERE all(r IN rels WHERE r.user_id = $user_id AND NOT r.status IN $excluded_statuses)
  AND all(n IN nodes(path) WHERE n.user_id = $user_id)
  AND other.id <> $entity_id
WITH other, path, length(path) AS len
ORDER BY len ASC
WITH other, collect(path)[0] AS shortest_path, min(len) AS hops
RETURN other {{.*}} AS entity, hops,
       [n IN nodes(shortest_path) | n {{.*}}] AS path_nodes,
       [r IN relationships(shortest_path) | r {{.*, source_entity_id: startNode(r).id,
                                                   target_entity_id: endNode(r).id}}] AS path_rels
ORDER BY hops, other.name
LIMIT $max_nodes
"""

#: The canonical entity-resolution mechanism (ADR 0010, PHASE4_PLAN.md section
#: 9). `WHERE canonical IS NULL OR NOT (canonical)-[:MERGED_INTO]->()` keeps
#: either "no redirect exists at all" (the ordinary case) or a truly canonical
#: endpoint (not itself further redirected) -- a cycle within the bound simply
#: satisfies neither for any candidate, which collapses back to the "no
#: redirect" row: never an infinite loop, never an error, never a wrong answer.
#: `ORDER BY depth DESC LIMIT 1` picks the longest (most-resolved) valid chain.
RESOLVE_ENTITY = f"""
MATCH (start:Entity {{id: $entity_id, user_id: $user_id}})
OPTIONAL MATCH redirect_path = (start)-[:MERGED_INTO*1..{_MAX_REDIRECT_DEPTH}]->
               (canonical:Entity {{user_id: $user_id}})
WHERE canonical IS NULL OR NOT (canonical)-[:MERGED_INTO]->()
WITH start, coalesce(canonical, start) AS resolved,
     CASE WHEN redirect_path IS NULL THEN 0 ELSE length(redirect_path) END AS depth
ORDER BY depth DESC
LIMIT 1
RETURN resolved {{.*}} AS entity, start.id AS original_id, depth AS redirect_depth
"""

#: Every outgoing/incoming RELATES_TO edge on an entity, read once before a
#: merge so the caller (Python) can assign fresh ids to the copies it will ask
#: Neo4j to create -- id generation stays in application code, matching every
#: other write path in this codebase (never generated inside Cypher).
GET_ENTITY_EDGES_FOR_MERGE = """
MATCH (b:Entity {id: $entity_id, user_id: $user_id})-[r:RELATES_TO {user_id: $user_id}]->
      (other:Entity {user_id: $user_id})
WHERE NOT r.status IN $excluded_statuses
RETURN 'outgoing' AS direction, other.id AS other_id, r {.*} AS rel
UNION ALL
MATCH (other:Entity {user_id: $user_id})-[r:RELATES_TO {user_id: $user_id}]->
      (b:Entity {id: $entity_id, user_id: $user_id})
WHERE NOT r.status IN $excluded_statuses
RETURN 'incoming' AS direction, other.id AS other_id, r {.*} AS rel
"""

#: Step 1 of MERGE_ENTITY: the redirect edge itself. `WHERE NOT (a)-[:MERGED_INTO*1..N]->(b)`
#: refuses to create a redirect that would close a cycle -- checked as part of
#: the same transaction as the rest of the merge, not a separate race-prone
#: pre-check. MERGE (not CREATE) on the edge's identifying properties makes a
#: repeated merge attempt idempotent at the Neo4j layer too (the primary guard
#: is the PostgreSQL review-candidate status, checked before this ever runs --
#: this is defense in depth, the same layered idempotency extraction already uses).
CREATE_MERGE_REDIRECT = f"""
MATCH (b:Entity {{id: $entity_id_b, user_id: $user_id}})
MATCH (a:Entity {{id: $entity_id_a, user_id: $user_id}})
WHERE NOT (a)-[:MERGED_INTO*1..{_MAX_REDIRECT_DEPTH}]->(b)
MERGE (b)-[m:MERGED_INTO {{user_id: $user_id}}]->(a)
ON CREATE SET m.created_at = $now, m.merge_id = $merge_id
RETURN b.id AS b_id, a.id AS a_id, m.merge_id AS merge_id
"""

#: Step 2: union B's name into A's aliases -- identical logic to MERGE_ENTITIES'
#: own ON MATCH alias union (memory_repository.py), applied once, explicitly, here.
UNION_ALIASES_FOR_MERGE = """
MATCH (a:Entity {id: $entity_id_a, user_id: $user_id})
MATCH (b:Entity {id: $entity_id_b, user_id: $user_id})
SET a.aliases = [x IN coalesce(a.aliases, []) WHERE NOT x IN $new_aliases] + $new_aliases,
    a.updated_at = $now
RETURN a.id AS id
"""

#: Step 3: create A's copy of each of B's edges, tagged `merged_via` so a
#: reversal (REVERSE_MERGE) can find and remove exactly these and nothing else.
#: MERGE on (user_id, memory_id, merged_via) -- unique per original assertion
#: per merge -- makes re-running this step idempotent rather than duplicating.
COPY_RELATIONSHIPS_FOR_MERGE = """
UNWIND $copies AS c
MATCH (src:Entity {id: c.source_id, user_id: $user_id})
MATCH (tgt:Entity {id: c.target_id, user_id: $user_id})
MERGE (src)-[copy:RELATES_TO {user_id: $user_id, memory_id: c.memory_id,
                               merged_via: $merge_id}]->(tgt)
ON CREATE SET copy.id = c.id,
              copy.predicate = c.predicate,
              copy.status = c.status,
              copy.confidence = c.confidence,
              copy.valid_from = c.valid_from,
              copy.valid_until = c.valid_until,
              copy.created_at = $now,
              copy.updated_at = $now
RETURN count(copy) AS created
"""

#: Reversal: delete exactly the edges one merge created (tagged `merged_via`),
#: then the redirect itself -- precise because the tag is precise. B's own
#: original relationships were never touched by the merge, so nothing here
#: needs to restore them.
REVERSE_MERGE_EDGES = """
MATCH ()-[r:RELATES_TO {user_id: $user_id, merged_via: $merge_id}]->()
DELETE r
RETURN count(r) AS removed
"""

REVERSE_MERGE_REDIRECT = """
MATCH (b:Entity {id: $entity_id_b, user_id: $user_id})
             -[m:MERGED_INTO {user_id: $user_id, merge_id: $merge_id}]->
             (a:Entity {id: $entity_id_a, user_id: $user_id})
DELETE m
RETURN b.id AS b_id, a.id AS a_id
"""

USER_SCOPED_STATEMENTS: tuple[str, ...] = (
    GET_ENTITY,
    FIND_ENTITY_BY_NAME,
    SEARCH_ENTITIES,
    LIST_ALL_ENTITIES,
    GET_RELATIONSHIPS,
    GET_ENTITY_MEMORIES,
    GET_MEMORIES_FOR_ENTITIES,
    NEIGHBOURHOOD_NODES_TEMPLATE,
    NEIGHBOURHOOD_EDGES,
    NEIGHBOURHOOD_PATHS_TEMPLATE,
    RESOLVE_ENTITY,
    GET_ENTITY_EDGES_FOR_MERGE,
    CREATE_MERGE_REDIRECT,
    UNION_ALIASES_FOR_MERGE,
    COPY_RELATIONSHIPS_FOR_MERGE,
    REVERSE_MERGE_EDGES,
    REVERSE_MERGE_REDIRECT,
)


def validate_hops(hops: Any, max_hops: int) -> int:
    """Return `hops` as a safe integer for interpolation into a Cypher path bound.

    Cypher does not allow a variable-length bound to be parameterised, so this value
    is the one piece of a query built by string formatting. It is therefore checked
    strictly: it must be an integer (bool is rejected -- `True` is an int in Python)
    within [1, max_hops]. Anything else raises rather than reaching the database.
    """
    if isinstance(hops, bool) or not isinstance(hops, int):
        raise ValidationError(
            "hops must be an integer", details={"received": repr(hops)}
        )
    if hops < 1 or hops > max_hops:
        raise ValidationError(
            f"hops must be between 1 and {max_hops}",
            details={"received": hops, "max_hops": max_hops},
        )
    return hops


class EntityRepository:
    """Graph reads for entities and their neighbourhoods."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def get(self, *, user_id: str, entity_id: str) -> EntityDetail | None:
        rows = await self._client.read(
            GET_ENTITY, {"user_id": user_id, "entity_id": entity_id}
        )
        if not rows or rows[0].get("entity") is None:
            return None
        row = rows[0]
        return EntityDetail(
            entity=to_entity(row["entity"]),
            memory_count=int(row["memory_count"]),
            relationship_count=int(row["relationship_count"]),
        )

    async def find_by_normalized_name(
        self, *, user_id: str, normalized_name: str
    ) -> Entity | None:
        rows = await self._client.read(
            FIND_ENTITY_BY_NAME,
            {"user_id": user_id, "normalized_name": normalized_name},
        )
        return to_entity(rows[0]["entity"]) if rows else None

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        offset: int,
        include_merged: bool = False,
    ) -> list[Entity]:
        rows = await self._client.read(
            SEARCH_ENTITIES,
            {
                "user_id": user_id,
                "query": query,
                "limit": limit,
                "offset": offset,
                "include_merged": include_merged,
            },
        )
        return [to_entity(r["entity"]) for r in rows]

    async def list_all(self, *, user_id: str, limit: int) -> list[Entity]:
        """Every entity for a user, bounded. See LIST_ALL_ENTITIES."""
        rows = await self._client.read(
            LIST_ALL_ENTITIES, {"user_id": user_id, "limit": limit}
        )
        return [to_entity(r["entity"]) for r in rows]

    async def relationships(
        self,
        *,
        user_id: str,
        entity_id: str,
        direction: Direction,
        excluded_statuses: list[str],
    ) -> list[RelationshipView]:
        rows = await self._client.read(
            GET_RELATIONSHIPS,
            {
                "user_id": user_id,
                "entity_id": entity_id,
                "excluded_statuses": excluded_statuses,
            },
        )
        views: list[RelationshipView] = []
        for row in rows:
            rel = to_relationship(row["relationship"])
            if direction is Direction.OUTGOING and rel.source_entity_id != entity_id:
                continue
            if direction is Direction.INCOMING and rel.target_entity_id != entity_id:
                continue
            views.append(
                RelationshipView(
                    relationship=rel,
                    source_entity=to_entity(row["source_entity"]),
                    target_entity=to_entity(row["target_entity"]),
                )
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
        rows = await self._client.read(
            GET_ENTITY_MEMORIES,
            {
                "user_id": user_id,
                "entity_id": entity_id,
                "excluded_statuses": excluded_statuses,
                "limit": limit,
                "offset": offset,
            },
        )
        return [to_memory(r["memory"]) for r in rows]

    async def memories_for_entities(
        self,
        *,
        user_id: str,
        entity_ids: list[str],
        excluded_statuses: list[str],
        limit_per_entity: int,
    ) -> dict[str, list[Memory]]:
        """`{entity_id: [memory, ...]}` for a whole set of entities in one
        round trip (PHASE4_PLAN.md section 14) -- see `GET_MEMORIES_FOR_ENTITIES`.
        An entity with no memories (or not found at all) is simply absent from
        the returned dict rather than mapped to an empty list.
        """
        if not entity_ids:
            return {}
        rows = await self._client.read(
            GET_MEMORIES_FOR_ENTITIES,
            {
                "user_id": user_id,
                "entity_ids": entity_ids,
                "excluded_statuses": excluded_statuses,
                "limit_per_entity": limit_per_entity,
            },
        )
        return {
            row["entity_id"]: [to_memory(m) for m in row["memories"]]
            for row in rows
            if row["memories"]
        }

    async def neighbourhood(
        self,
        *,
        user_id: str,
        root: Entity,
        hops: int,
        max_hops: int,
        excluded_statuses: list[str],
        max_nodes: int,
        max_edges: int,
    ) -> Neighborhood:
        """`max_nodes`/`max_edges` (Phase 8, D1) cap the result; one extra row
        is fetched for each so `Neighborhood.truncated` is exact."""
        safe_hops = validate_hops(hops, max_hops)
        query = NEIGHBOURHOOD_NODES_TEMPLATE.format(hops=safe_hops)
        node_rows = await self._client.read(
            query,
            {
                "user_id": user_id,
                "entity_id": root.id,
                "excluded_statuses": excluded_statuses,
                "max_nodes": max_nodes + 1,
            },
        )
        truncated = len(node_rows) > max_nodes
        nodes = [
            GraphNode(entity=to_entity(r["entity"]), hops=int(r["hops"]))
            for r in node_rows[:max_nodes]
        ]
        ids = [root.id, *[n.entity.id for n in nodes]]
        edge_rows = await self._client.read(
            NEIGHBOURHOOD_EDGES,
            {
                "user_id": user_id,
                "ids": ids,
                "excluded_statuses": excluded_statuses,
                "max_edges": max_edges + 1,
            },
        )
        truncated = truncated or len(edge_rows) > max_edges
        relationships: list[Relationship] = [
            to_relationship(r["relationship"]) for r in edge_rows[:max_edges]
        ]
        return Neighborhood(
            root=root,
            hops=safe_hops,
            nodes=nodes,
            relationships=relationships,
            truncated=truncated,
        )

    async def neighbourhood_with_paths(
        self,
        *,
        user_id: str,
        root: Entity,
        hops: int,
        max_hops: int,
        excluded_statuses: list[str],
        max_nodes: int,
    ) -> list[GraphNodeWithPath]:
        """Bounded multi-hop discovery with the real shortest path captured per
        node (PHASE4_PLAN.md sections 1/4) -- used by retrieval, not the
        interactive neighbourhood endpoint (which uses `neighbourhood` above,
        unchanged).
        """
        safe_hops = validate_hops(hops, max_hops)
        query = NEIGHBOURHOOD_PATHS_TEMPLATE.format(hops=safe_hops)
        rows = await self._client.read(
            query,
            {
                "user_id": user_id,
                "entity_id": root.id,
                "excluded_statuses": excluded_statuses,
                "max_nodes": max_nodes,
            },
        )
        results: list[GraphNodeWithPath] = []
        for row in rows:
            path_nodes = row["path_nodes"]
            path_rels = row["path_rels"]
            steps = [
                GraphPathStep(entity_id=path_nodes[0]["id"], entity_name=path_nodes[0]["name"])
            ]
            for i, rel in enumerate(path_rels):
                node = path_nodes[i + 1]
                previous_id = path_nodes[i]["id"]
                direction = "outgoing" if rel["source_entity_id"] == previous_id else "incoming"
                steps.append(
                    GraphPathStep(
                        entity_id=node["id"], entity_name=node["name"],
                        predicate=rel.get("predicate"), direction=direction,
                    )
                )
            results.append(
                GraphNodeWithPath(
                    entity=to_entity(row["entity"]), hops=int(row["hops"]), path=steps
                )
            )
        return results

    async def resolve(self, *, user_id: str, entity_id: str) -> ResolvedEntity | None:
        """The one canonical entity-resolution mechanism (ADR 0010,
        PHASE4_PLAN.md section 9). Returns `None` only if `entity_id` does not
        exist for this user at all; a non-redirected entity resolves to itself
        with `redirect_depth=0`.
        """
        rows = await self._client.read(
            RESOLVE_ENTITY, {"user_id": user_id, "entity_id": entity_id}
        )
        if not rows or rows[0].get("entity") is None:
            return None
        row = rows[0]
        depth = int(row["redirect_depth"])
        return ResolvedEntity(
            entity=to_entity(row["entity"]),
            original_id=row["original_id"],
            redirect_depth=depth,
            was_redirected=depth > 0,
        )

    async def merge(
        self,
        *,
        user_id: str,
        entity_id_a: str,
        entity_id_b: str,
        merge_id: str,
        new_aliases: list[str],
        excluded_statuses: list[str],
        now: datetime,
        id_fn: Any,
    ) -> None:
        """Consolidate B into A: B's relationships are copied (never moved) to
        A, B's name is unioned into A's aliases, and `(B)-[:MERGED_INTO]->(A)`
        is created (PHASE4_PLAN.md section 9). B itself is never deleted or
        mutated beyond this redirect edge.

        Raises `ConflictError` if creating this redirect would close a cycle
        (`CREATE_MERGE_REDIRECT`'s own `WHERE NOT (a)-[:MERGED_INTO*]->(b)`
        guard found one -- checked inside the same transaction as the rest of
        the merge, not a separate race-prone pre-check). Raises `NotFoundError`
        if either entity does not exist for this user.
        """
        edge_rows = await self._client.read(
            GET_ENTITY_EDGES_FOR_MERGE,
            {"user_id": user_id, "entity_id": entity_id_b, "excluded_statuses": excluded_statuses},
        )
        copies: list[dict[str, Any]] = []
        for row in edge_rows:
            rel = row["rel"]
            if row["direction"] == "outgoing":
                source_id, target_id = entity_id_a, row["other_id"]
            else:
                source_id, target_id = row["other_id"], entity_id_a
            copies.append(
                {
                    "id": id_fn(),
                    "source_id": source_id,
                    "target_id": target_id,
                    "memory_id": rel.get("memory_id"),
                    "predicate": rel.get("predicate"),
                    "status": rel.get("status"),
                    "confidence": rel.get("confidence"),
                    "valid_from": rel.get("valid_from"),
                    "valid_until": rel.get("valid_until"),
                }
            )

        def check_redirect_created(rows: list[dict[str, Any]]) -> None:
            if not rows:
                raise ConflictError(
                    "Merging these entities would create a redirect cycle, or one "
                    "of them does not exist for this user",
                    details={"entity_id_a": entity_id_a, "entity_id_b": entity_id_b},
                )

        common = {"user_id": user_id, "now": now, "merge_id": merge_id}
        steps = [
            TxStep(
                CREATE_MERGE_REDIRECT,
                {**common, "entity_id_a": entity_id_a, "entity_id_b": entity_id_b},
                check=check_redirect_created,
            ),
            TxStep(
                UNION_ALIASES_FOR_MERGE,
                {
                    **common, "entity_id_a": entity_id_a, "entity_id_b": entity_id_b,
                    "new_aliases": new_aliases,
                },
            ),
        ]
        if copies:
            steps.append(TxStep(COPY_RELATIONSHIPS_FOR_MERGE, {**common, "copies": copies}))
        await self._client.write_transaction(steps)

    async def reverse_merge(
        self, *, user_id: str, entity_id_a: str, entity_id_b: str, merge_id: str
    ) -> None:
        """Undo exactly one merge: delete the edges it created (tagged
        `merged_via=merge_id`) and the redirect itself. B's alias addition is
        NOT retracted (PHASE4_PLAN.md section 10 -- a stated, accepted
        limitation: B may have accumulated other aliases since, so blindly
        removing one string is not safe). Raises `NotFoundError` if the
        redirect edge for this exact `merge_id` does not exist.
        """
        common = {"user_id": user_id, "merge_id": merge_id}

        def check_redirect_removed(rows: list[dict[str, Any]]) -> None:
            if not rows:
                raise NotFoundError(
                    "No such merge to reverse", details={"merge_id": merge_id}
                )

        steps = [
            TxStep(REVERSE_MERGE_EDGES, common),
            TxStep(
                REVERSE_MERGE_REDIRECT,
                {**common, "entity_id_a": entity_id_a, "entity_id_b": entity_id_b},
                check=check_redirect_removed,
            ),
        ]
        await self._client.write_transaction(steps)


def as_of_or_now(as_of: datetime | None, now: datetime) -> datetime:
    """Resolve an optional as-of instant to a concrete one."""
    return as_of if as_of is not None else now
