"""Memory persistence.

Every statement in this module filters on `$user_id`. That is the single enforcement
point for the isolation requirement in ARCHITECTURE.md section 15 and CLAUDE.md
section 26, and `tests/unit/test_cypher_safety.py` statically asserts it for every
query constant here, so a future query that forgets the filter fails the test suite
rather than leaking another user's memory.

Cypher is kept in module-level constants (rather than inline f-strings) precisely so
it can be audited that way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.errors import ConflictError, NotFoundError
from app.graph.connection import GraphClient, TxStep
from app.models.domain import Memory, MemoryDetail
from app.models.enums import MemoryStatus
from app.repositories.mapping import to_entity, to_memory, to_relationship, to_source

# --------------------------------------------------------------------------
# Write statements. Steps 1-5 run inside one transaction (see GraphClient
# .write_transaction) so that a memory is never half-created.
# --------------------------------------------------------------------------

#: `user_id` is assigned from its own parameter rather than relied upon inside
#: `$memory`, so that the authoritative owner of a new node comes from the same
#: request-scoped parameter as every other statement here -- and so the isolation
#: audit in tests/unit/test_cypher_safety.py can verify it by inspection.
CREATE_MEMORY = """
CREATE (m:Memory)
SET m = $memory,
    m.user_id = $user_id
RETURN m.id AS id
"""

#: Upsert the mentioned entities and link them to the memory.
#: `entity_type` is deliberately NOT overwritten on match: the first assertion wins,
#: because silently retyping an existing entity would rewrite history that the user
#: never asked to change. Aliases are unioned rather than replaced.
MERGE_ENTITIES = """
UNWIND $entities AS ent
MERGE (e:Entity {user_id: $user_id, normalized_name: ent.normalized_name})
  ON CREATE SET e.id = ent.id,
                e.name = ent.name,
                e.entity_type = ent.entity_type,
                e.aliases = ent.aliases,
                e.created_at = $now,
                e.updated_at = $now
  ON MATCH SET e.updated_at = $now,
               e.aliases = [a IN coalesce(e.aliases, []) WHERE NOT a IN ent.aliases]
                           + ent.aliases
WITH e, ent
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
MERGE (m)-[:MENTIONS]->(e)
RETURN e.id AS id, e.normalized_name AS normalized_name
"""

#: Sources are deduplicated per user on (source_type, dedup_key) so that many memories
#: extracted from the same conversation or document share one Source node -- which is
#: what lets an entity page report "3 conversations, 2 documents".
MERGE_SOURCE = """
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
MERGE (s:Source {user_id: $user_id, source_type: $source.source_type,
                 dedup_key: $source.dedup_key})
  ON CREATE SET s.id = $source.id,
                s.reference = $source.reference,
                s.location = $source.location,
                s.created_at = $now
MERGE (m)-[:DERIVED_FROM]->(s)
RETURN s.id AS id
"""

#: Relationship edges are CREATEd, not MERGEd: each assertion carries its own
#: provenance (`memory_id`) and its own validity window, and collapsing two
#: assertions into one edge would destroy exactly the history this system exists to
#: keep. Recognising that two assertions are duplicates is Phase 4's duplicate
#: detection, not a storage concern.
CREATE_RELATIONSHIPS = """
UNWIND $relationships AS rel
MATCH (src:Entity {user_id: $user_id, normalized_name: rel.source_normalized})
MATCH (tgt:Entity {user_id: $user_id, normalized_name: rel.target_normalized})
CREATE (src)-[r:RELATES_TO]->(tgt)
SET r = rel.props
RETURN r.id AS id
"""

#: Close out superseded memories. `coalesce` preserves an explicitly-set valid_until
#: rather than overwriting the caller's stated end date. The old fact ends when the
#: new one became true (`$superseded_at`, its valid_from -- Phase 14), not when the
#: write happened; if that instant is not after the old fact's own start (facts
#: processed out of order), fall back to `$now` rather than write an inverted window.
SUPERSEDE_MEMORIES = """
UNWIND $supersedes AS old_id
MATCH (old:Memory {id: old_id, user_id: $user_id})
MATCH (new:Memory {id: $memory_id, user_id: $user_id})
SET old.status = $superseded_status,
    old.valid_until = coalesce(
        old.valid_until,
        CASE WHEN old.valid_from IS NULL OR $superseded_at > old.valid_from
             THEN $superseded_at ELSE $now END
    ),
    old.updated_at = $now
MERGE (new)-[:SUPERSEDES]->(old)
RETURN collect(DISTINCT old.id) AS superseded_ids
"""

SUPERSEDE_RELATIONSHIPS = """
UNWIND $supersedes AS old_id
MATCH ()-[r:RELATES_TO {user_id: $user_id, memory_id: old_id}]->()
SET r.status = $superseded_status,
    r.valid_until = coalesce(
        r.valid_until,
        CASE WHEN r.valid_from IS NULL OR $superseded_at > r.valid_from
             THEN $superseded_at ELSE $now END
    ),
    r.updated_at = $now
RETURN count(r) AS closed
"""

UPDATE_MEMORY = """
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
SET m += $changes,
    m.updated_at = $now
RETURN m {.*} AS memory
"""

INVALIDATE_MEMORY = """
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
SET m.status = $status,
    m.invalidation_reason = $reason,
    m.valid_until = coalesce(m.valid_until, $now),
    m.updated_at = $now
RETURN m {.*} AS memory
"""

INVALIDATE_RELATIONSHIPS = """
MATCH ()-[r:RELATES_TO {user_id: $user_id, memory_id: $memory_id}]->()
SET r.status = $status,
    r.valid_until = coalesce(r.valid_until, $now),
    r.updated_at = $now
RETURN count(r) AS closed
"""

# --------------------------------------------------------------------------
# Read statements
# --------------------------------------------------------------------------

#: `sources` is many-to-many (Phase 2): a DUPLICATE-classified candidate attaches
#: an additional Source to an existing memory via `attach_source` below, so a
#: memory can have more than one. The original `head(collect(...))` singular form
#: from Phase 1 is gone -- see docs/PHASE2_PLAN.md section 2's Neo4j additions and
#: `attach_source`'s docstring.
GET_MEMORY = """
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity {user_id: $user_id})
OPTIONAL MATCH (m)-[:DERIVED_FROM]->(s:Source {user_id: $user_id})
OPTIONAL MATCH (a:Entity {user_id: $user_id})-[r:RELATES_TO {user_id: $user_id,
               memory_id: $memory_id}]->(b:Entity {user_id: $user_id})
OPTIONAL MATCH (m)-[:SUPERSEDES]->(older:Memory {user_id: $user_id})
OPTIONAL MATCH (newer:Memory {user_id: $user_id})-[:SUPERSEDES]->(m)
OPTIONAL MATCH (m)-[:CONFLICTS_WITH]-(conflicting:Memory {user_id: $user_id})
OPTIONAL MATCH (m)-[:DUPLICATE_OF]->(canonical:Memory {user_id: $user_id})
OPTIONAL MATCH (dup:Memory {user_id: $user_id})-[:DUPLICATE_OF]->(m)
RETURN m {.*} AS memory,
       collect(DISTINCT e {.*}) AS entities,
       collect(DISTINCT s {.*}) AS sources,
       collect(DISTINCT r {.*, source_entity_id: a.id, target_entity_id: b.id})
           AS relationships,
       collect(DISTINCT older.id) AS supersedes,
       collect(DISTINCT newer.id) AS superseded_by,
       collect(DISTINCT conflicting.id) AS conflicts_with,
       collect(DISTINCT canonical.id) AS duplicate_of,
       collect(DISTINCT dup.id) AS duplicates
"""

#: Base of the search query. Filters are appended from a fixed vocabulary of
#: fragments; all caller-supplied values travel as parameters.
SEARCH_MEMORIES_BASE = "MATCH (m:Memory {user_id: $user_id})"

SEARCH_MEMORIES_RETURN = """
RETURN m {.*} AS memory
ORDER BY m.created_at DESC, m.id
SKIP $offset LIMIT $limit
"""

COUNT_MEMORIES_RETURN = "RETURN count(m) AS total"

#: Filter fragments. `as_of` mirrors `app.models.lifecycle.is_valid_at`: the
#: half-open interval [valid_from, valid_until).
FILTER_FRAGMENTS: dict[str, str] = {
    "query": "toLower(m.content) CONTAINS toLower($query)",
    "statuses": "m.status IN $statuses",
    "memory_types": "m.memory_type IN $memory_types",
    "as_of": (
        "(m.valid_from IS NULL OR m.valid_from <= $as_of) "
        "AND (m.valid_until IS NULL OR m.valid_until > $as_of)"
    ),
    "entity_normalized_name": (
        "EXISTS { (m)-[:MENTIONS]->(:Entity {user_id: $user_id, "
        "normalized_name: $entity_normalized_name}) }"
    ),
}

#: Every write/read constant that must be user-scoped. Audited by the test suite.
#: Phase 2: attach an additional Source to an already-existing memory (the
#: DUPLICATE decision, docs/PHASE2_PLAN.md section 4). Deduplicates on the same
#: (user_id, source_type, dedup_key) key as MERGE_SOURCE, so a second candidate
#: from the same conversation still shares one Source node.
ATTACH_SOURCE = """
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
MERGE (s:Source {user_id: $user_id, source_type: $source.source_type,
                 dedup_key: $source.dedup_key})
  ON CREATE SET s.id = $source.id,
                s.reference = $source.reference,
                s.location = $source.location,
                s.created_at = $now
MERGE (m)-[:DERIVED_FROM]->(s)
RETURN s.id AS id
"""

#: Phase 2: flag two memories as contradicting each other (the CONFLICTING
#: decision). A single directed edge -- read in either direction by GET_MEMORY's
#: `-[:CONFLICTS_WITH]-` pattern -- since the relationship is symmetric and Neo4j
#: has no undirected edge type. MERGE (not CREATE) so re-flagging the same pair is
#: a no-op rather than a growing multi-edge.
LINK_CONFLICTING = """
MATCH (a:Memory {id: $memory_id_a, user_id: $user_id})
MATCH (b:Memory {id: $memory_id_b, user_id: $user_id})
MERGE (a)-[c:CONFLICTS_WITH]-(b)
ON CREATE SET c.created_at = $now, c.user_id = $user_id
RETURN a.id AS a_id, b.id AS b_id
"""

#: Phase 4 (ADR 0010, PHASE4_PLAN.md section 8): a human-confirmed duplicate.
#: `dup` is never deleted or rewritten -- it keeps its content, sources and
#: relationships exactly as extracted, only gaining a status and an edge
#: pointing at the memory a reviewer picked as canonical. The guard clause
#: makes this MERGE-idempotent for a repeated confirm of the SAME pair (a
#: no-op re-match) while still rejecting an attempt to point `dup` at a
#: DIFFERENT canonical once it is already merged somewhere -- the Postgres
#: review-candidate status is the primary gate against that; this is
#: defense in depth, the same layered idempotency extraction already uses.
CONFIRM_MEMORY_DUPLICATE = """
MATCH (dup:Memory {id: $duplicate_id, user_id: $user_id})
MATCH (canonical:Memory {id: $canonical_id, user_id: $user_id})
WHERE NOT EXISTS {
    (dup)-[:DUPLICATE_OF]->(existing:Memory) WHERE existing.id <> canonical.id
}
MERGE (dup)-[d:DUPLICATE_OF {user_id: $user_id}]->(canonical)
ON CREATE SET d.created_at = $now
SET dup.status = $merged_status,
    dup.updated_at = $now
RETURN dup.id AS duplicate_id, canonical.id AS canonical_id
"""

#: Phase 4: a reviewer resolves a CONFLICTING pair by deciding `winner`
#: (currently CONFLICTING, becomes ACTIVE) supersedes `loser` (its own status
#: transitions via the existing SUPERSEDE_MEMORIES semantics -- reused as a
#: second step, not duplicated here). The CONFLICTS_WITH edge between them is
#: left in place: it is a true historical fact ("these two were once in
#: conflict"), and the resolution is recorded by each memory's own status plus
#: the PostgreSQL review audit trail, not by erasing the edge.
RESOLVE_CONFLICT_SUPERSEDE_WINNER = """
MATCH (winner:Memory {id: $winner_id, user_id: $user_id})
SET winner.status = $active_status,
    winner.updated_at = $now
RETURN winner.id AS winner_id
"""

#: Phase 4: a reviewer decides the CONFLICTING flag itself was wrong (both
#: memories genuinely hold, or the classification was a mistake) -- the
#: flagged memory returns to ACTIVE. Nothing is superseded and the
#: CONFLICTS_WITH edge is left in place for the same reason as above.
RESOLVE_CONFLICT_REJECT = """
MATCH (m:Memory {id: $memory_id, user_id: $user_id})
SET m.status = $active_status,
    m.updated_at = $now
RETURN m.id AS memory_id
"""

#: Existence-only check, used by `confirm_duplicate` to distinguish "one of
#: these memories does not exist" from "CONFIRM_MEMORY_DUPLICATE's guard
#: rejected a conflicting target" when that write returns zero rows -- kept as
#: its own audited constant rather than an inline string (this module's own
#: stated discipline).
CHECK_MEMORIES_EXIST = """
MATCH (m:Memory {user_id: $user_id})
WHERE m.id IN $ids
RETURN m.id AS id
"""

#: Phase 3 (ADR 0009, PHASE3_PLAN.md section 13): a bounded page of this user's
#: memories, for the embedding backfill sweep to scan for missing embeddings. No
#: Neo4j schema change -- reads only what already exists. Ordered oldest-first:
#: a newly-created memory already gets embedded synchronously right after
#: extraction, or via a background task on the direct-create route, so the
#: backfill's job is primarily historical (pre-Phase-3) memories and anything a
#: live embed attempt failed for. Keyset-paginated by id (Phase 11, J11): the
#: previous oldest-first `LIMIT` re-scanned the same first page on every tick,
#: so memories beyond it were never reached.
LIST_MEMORIES_FOR_BACKFILL = """
MATCH (m:Memory {user_id: $user_id})
WHERE $after_id IS NULL OR m.id > $after_id
RETURN m.id AS id, m.content AS content
ORDER BY m.id ASC
LIMIT $limit
"""

#: Phase 14 (M3): which of these names belong to an entity that was merged
#: away, and into what. A write naming "Open AI" after "Open AI" was merged into
#: "OpenAI" used to MERGE onto the merged-away node, so the new fact was
#: invisible from the canonical entity. Bounded (depth <= 5, as RESOLVE_ENTITY)
#: and user-scoped on every node.
RESOLVE_MERGED_NAMES = """
UNWIND $names AS name
MATCH (e:Entity {user_id: $user_id, normalized_name: name})-[:MERGED_INTO*1..5]->
      (c:Entity {user_id: $user_id})
WHERE NOT (c)-[:MERGED_INTO]->()
RETURN name, c.name AS canonical_name, c.normalized_name AS canonical_normalized
"""

#: Phase 14 (M8): the keyword channel's term match -- memories containing any of
#: a few stemmed query terms. Bounded by $limit; the same per-user content scan
#: the whole-query CONTAINS search already does.
SEARCH_BY_TERMS = """
MATCH (m:Memory {user_id: $user_id})
WHERE any(t IN $terms WHERE toLower(m.content) CONTAINS t)
RETURN m.id AS id, toLower(m.content) AS content
ORDER BY m.created_at DESC, m.id
LIMIT $limit
"""

USER_SCOPED_STATEMENTS: tuple[str, ...] = (
    RESOLVE_MERGED_NAMES,
    SEARCH_BY_TERMS,
    CREATE_MEMORY,
    MERGE_ENTITIES,
    MERGE_SOURCE,
    CREATE_RELATIONSHIPS,
    SUPERSEDE_MEMORIES,
    SUPERSEDE_RELATIONSHIPS,
    UPDATE_MEMORY,
    INVALIDATE_MEMORY,
    INVALIDATE_RELATIONSHIPS,
    GET_MEMORY,
    SEARCH_MEMORIES_BASE,
    ATTACH_SOURCE,
    LINK_CONFLICTING,
    LIST_MEMORIES_FOR_BACKFILL,
    CONFIRM_MEMORY_DUPLICATE,
    RESOLVE_CONFLICT_SUPERSEDE_WINNER,
    RESOLVE_CONFLICT_REJECT,
    CHECK_MEMORIES_EXIST,
)


class MemoryRepository:
    """Graph persistence for memories, their entities, sources and edges."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

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
        superseded_at: datetime | None = None,
    ) -> None:
        """Write a complete memory in a single transaction.

        Raises NotFoundError (rolling the transaction back) if any memory listed in
        `supersedes` does not exist for this user.
        """
        memory_id = memory_props["id"]
        common = {"user_id": user_id, "memory_id": memory_id, "now": now}

        def verify_all_superseded(rows: list[dict[str, Any]]) -> None:
            """Reject the write if any memory named in `supersedes` was not matched.

            Runs inside the transaction, so raising rolls back the memory, entity and
            relationship writes above it rather than leaving them committed.
            """
            found = set(rows[0]["superseded_ids"]) if rows else set()
            missing = sorted(set(supersedes) - found)
            if missing:
                raise NotFoundError(
                    "One or more memories to supersede do not exist",
                    details={"missing_memory_ids": missing},
                )

        steps: list[TxStep] = [
            TxStep(CREATE_MEMORY, {"memory": memory_props, "user_id": user_id}),
        ]
        if entities:
            steps.append(TxStep(MERGE_ENTITIES, {**common, "entities": entities}))
        if source:
            steps.append(TxStep(MERGE_SOURCE, {**common, "source": source}))
        if relationships:
            steps.append(TxStep(CREATE_RELATIONSHIPS, {**common, "relationships": relationships}))
        if supersedes:
            supersede_params = {
                **common,
                "supersedes": supersedes,
                "superseded_status": MemoryStatus.SUPERSEDED.value,
                "superseded_at": superseded_at or now,
            }
            steps.append(TxStep(SUPERSEDE_MEMORIES, supersede_params, check=verify_all_superseded))
            steps.append(TxStep(SUPERSEDE_RELATIONSHIPS, supersede_params))

        await self._client.write_transaction(steps)

    async def search_by_terms(
        self, *, user_id: str, terms: list[str], limit: int
    ) -> list[tuple[str, str]]:
        """(memory id, lowercased content) for memories containing any term."""
        if not terms:
            return []
        rows = await self._client.read(
            SEARCH_BY_TERMS, {"user_id": user_id, "terms": terms, "limit": limit}
        )
        return [(r["id"], r["content"]) for r in rows]

    async def resolve_merged_names(
        self, *, user_id: str, normalized_names: list[str]
    ) -> dict[str, tuple[str, str]]:
        """normalized name -> (canonical name, canonical normalized name), for
        the names that were merged away; names not merged are absent."""
        if not normalized_names:
            return {}
        rows = await self._client.read(
            RESOLVE_MERGED_NAMES, {"user_id": user_id, "names": normalized_names}
        )
        return {r["name"]: (r["canonical_name"], r["canonical_normalized"]) for r in rows}

    async def get(self, *, user_id: str, memory_id: str) -> MemoryDetail | None:
        rows = await self._client.read(GET_MEMORY, {"user_id": user_id, "memory_id": memory_id})
        if not rows or rows[0].get("memory") is None:
            return None
        row = rows[0]
        return MemoryDetail(
            memory=to_memory(row["memory"]),
            entities=[to_entity(e) for e in row["entities"] if e],
            relationships=[to_relationship(r) for r in row["relationships"] if r],
            sources=[to_source(s) for s in row["sources"] if s],
            supersedes=[i for i in row["supersedes"] if i],
            superseded_by=[i for i in row["superseded_by"] if i],
            conflicts_with=[i for i in row["conflicts_with"] if i],
            duplicate_of=[i for i in row["duplicate_of"] if i],
            duplicates=[i for i in row["duplicates"] if i],
        )

    async def attach_source(
        self,
        *,
        user_id: str,
        memory_id: str,
        source: dict[str, Any],
        now: datetime,
    ) -> None:
        """Add an additional Source to an existing memory (Phase 2 DUPLICATE).

        Does not touch the memory's content, status or validity window -- only
        provenance grows. Source dedup means a second candidate corroborating an
        already-recorded fact from the same conversation attaches to the same
        Source node rather than creating a new one.

        Raises NotFoundError if the memory does not exist for this user: an
        unmatched MATCH in Cypher affects zero rows and returns silently, so
        existence is checked explicitly rather than trusting a quiet no-op.
        """
        rows = await self._client.write(
            ATTACH_SOURCE,
            {"user_id": user_id, "memory_id": memory_id, "source": source, "now": now},
        )
        if not rows:
            raise NotFoundError("Memory not found", details={"memory_id": memory_id})

    async def link_conflicting(
        self, *, user_id: str, memory_id_a: str, memory_id_b: str, now: datetime
    ) -> None:
        """Flag two memories as contradicting each other (Phase 2 CONFLICTING).

        Both memories are left exactly as they are -- neither is invalidated,
        superseded, or rewritten. This only records that a human (or a later,
        better-evidenced phase) should look at the pair; see
        docs/adr/0007's conservative policy.

        Raises NotFoundError if either memory does not exist for this user.
        """
        rows = await self._client.write(
            LINK_CONFLICTING,
            {
                "user_id": user_id,
                "memory_id_a": memory_id_a,
                "memory_id_b": memory_id_b,
                "now": now,
            },
        )
        if not rows:
            raise NotFoundError(
                "One or both memories not found",
                details={"memory_id_a": memory_id_a, "memory_id_b": memory_id_b},
            )

    async def update(
        self,
        *,
        user_id: str,
        memory_id: str,
        changes: dict[str, Any],
        now: datetime,
    ) -> Memory | None:
        rows = await self._client.write(
            UPDATE_MEMORY,
            {"user_id": user_id, "memory_id": memory_id, "changes": changes, "now": now},
        )
        if not rows:
            return None
        return to_memory(rows[0]["memory"])

    async def invalidate(
        self,
        *,
        user_id: str,
        memory_id: str,
        reason: str | None,
        now: datetime,
        status: MemoryStatus = MemoryStatus.INVALID,
    ) -> Memory | None:
        """Mark a memory invalid and close the edges it asserted.

        The node is never deleted: retraction is recorded, not erased.
        """
        params = {
            "user_id": user_id,
            "memory_id": memory_id,
            "reason": reason,
            "now": now,
            "status": status.value,
        }
        results = await self._client.write_transaction(
            [TxStep(INVALIDATE_MEMORY, params), TxStep(INVALIDATE_RELATIONSHIPS, params)]
        )
        rows = results[0]
        if not rows:
            return None
        return to_memory(rows[0]["memory"])

    async def search(
        self,
        *,
        user_id: str,
        filters: dict[str, Any],
        limit: int,
        offset: int,
    ) -> list[Memory]:
        where, params = _build_filters(filters)
        query = f"{SEARCH_MEMORIES_BASE}{where}{SEARCH_MEMORIES_RETURN}"
        rows = await self._client.read(
            query, {"user_id": user_id, "limit": limit, "offset": offset, **params}
        )
        return [to_memory(r["memory"]) for r in rows]

    async def count(self, *, user_id: str, filters: dict[str, Any]) -> int:
        where, params = _build_filters(filters)
        query = f"{SEARCH_MEMORIES_BASE}{where}\n{COUNT_MEMORIES_RETURN}"
        rows = await self._client.read(query, {"user_id": user_id, **params})
        return int(rows[0]["total"]) if rows else 0

    async def list_for_backfill(
        self, *, user_id: str, limit: int, after_id: str | None = None
    ) -> list[dict[str, str]]:
        """A bounded, oldest-first page of `{id, content}` for the embedding
        backfill sweep (`app.semantic.embedding_pipeline`). Deliberately returns
        raw dicts, not `Memory` domain objects -- the backfill needs exactly two
        fields and nothing about status/validity, which is not its concern
        (PHASE3_PLAN.md section 3: temporal correctness is decided at retrieval
        time, never baked into what gets embedded).
        """
        rows = await self._client.read(
            LIST_MEMORIES_FOR_BACKFILL, {"user_id": user_id, "limit": limit, "after_id": after_id}
        )
        return [{"id": r["id"], "content": r["content"]} for r in rows]

    async def confirm_duplicate(
        self, *, user_id: str, duplicate_id: str, canonical_id: str, now: datetime
    ) -> None:
        """Record a human-confirmed duplicate (ADR 0010, PHASE4_PLAN.md section
        8): `duplicate_id` gets `status=MERGED` and a `DUPLICATE_OF` edge to
        `canonical_id`. Neither memory is deleted or rewritten -- see the
        query's own docstring for the idempotent-retry / conflicting-target
        distinction.

        Raises NotFoundError if either memory does not exist for this user, or
        ConflictError (via the query's guard finding zero rows for a bound
        `dup`) if `duplicate_id` is already a confirmed duplicate of a
        DIFFERENT memory.
        """
        rows = await self._client.write(
            CONFIRM_MEMORY_DUPLICATE,
            {
                "user_id": user_id,
                "duplicate_id": duplicate_id,
                "canonical_id": canonical_id,
                "now": now,
                "merged_status": MemoryStatus.MERGED.value,
            },
        )
        if not rows:
            # Distinguish "does not exist" from "guard rejected" with one cheap
            # extra read rather than parsing driver error text.
            existing = await self._client.read(
                CHECK_MEMORIES_EXIST,
                {"user_id": user_id, "ids": [duplicate_id, canonical_id]},
            )
            found = {r["id"] for r in existing}
            missing = {duplicate_id, canonical_id} - found
            if missing:
                raise NotFoundError(
                    "One or both memories not found",
                    details={"missing_memory_ids": sorted(missing)},
                )
            raise ConflictError(
                "This memory is already confirmed a duplicate of a different memory",
                details={"duplicate_id": duplicate_id},
            )

    async def resolve_conflict_supersede(
        self, *, user_id: str, winner_id: str, loser_id: str, now: datetime
    ) -> None:
        """A reviewer resolves a CONFLICTING pair: `winner` becomes ACTIVE,
        `loser` is superseded exactly like any other supersession (its own
        relationships close, a SUPERSEDES edge is created) -- reusing
        `SUPERSEDE_MEMORIES`/`SUPERSEDE_RELATIONSHIPS` rather than duplicating
        that logic. Raises NotFoundError if either memory does not exist.
        """

        def verify_loser_found(rows: list[dict[str, Any]]) -> None:
            if not rows or not rows[0]["superseded_ids"]:
                raise NotFoundError(
                    "One or both memories not found",
                    details={"winner_id": winner_id, "loser_id": loser_id},
                )

        common = {
            "user_id": user_id,
            "memory_id": winner_id,
            "supersedes": [loser_id],
            "now": now,
            # A reviewer's decision takes effect when it is made (unchanged).
            "superseded_at": now,
            "superseded_status": MemoryStatus.SUPERSEDED.value,
        }
        results = await self._client.write_transaction(
            [
                TxStep(
                    RESOLVE_CONFLICT_SUPERSEDE_WINNER,
                    {
                        "user_id": user_id,
                        "winner_id": winner_id,
                        "now": now,
                        "active_status": MemoryStatus.ACTIVE.value,
                    },
                ),
                TxStep(SUPERSEDE_MEMORIES, common, check=verify_loser_found),
                TxStep(SUPERSEDE_RELATIONSHIPS, common),
            ]
        )
        if not results[0]:
            raise NotFoundError("Memory not found", details={"winner_id": winner_id})

    async def resolve_conflict_reject(self, *, user_id: str, memory_id: str, now: datetime) -> None:
        """A reviewer decides a CONFLICTING flag was wrong: the memory returns
        to ACTIVE with nothing superseded. Raises NotFoundError if it does not
        exist for this user.
        """
        rows = await self._client.write(
            RESOLVE_CONFLICT_REJECT,
            {
                "user_id": user_id,
                "memory_id": memory_id,
                "now": now,
                "active_status": MemoryStatus.ACTIVE.value,
            },
        )
        if not rows:
            raise NotFoundError("Memory not found", details={"memory_id": memory_id})


def _build_filters(filters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Assemble a WHERE clause from the fixed fragment vocabulary.

    Only keys present in FILTER_FRAGMENTS contribute, and only as pre-written
    fragments; caller values are never interpolated into the query text.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for key, fragment in FILTER_FRAGMENTS.items():
        value = filters.get(key)
        if value is None or (isinstance(value, list | tuple) and not value):
            continue
        clauses.append(f"({fragment})")
        params[key] = list(value) if isinstance(value, tuple) else value
    if not clauses:
        return "", params
    return "\nWHERE " + "\n  AND ".join(clauses), params
