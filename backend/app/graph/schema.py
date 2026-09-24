"""Graph schema: constraints and indexes.

Applied explicitly by `scripts/init_graph.py` rather than implicitly at startup, so
that schema changes are a deliberate, observable operation (ARCHITECTURE.md
section 27).

Only features available in Neo4j Community Edition are used, so the same schema
applies to a local instance and to Neo4j Aura:

* node property uniqueness constraints (Community) -- used for ids and entity identity
* range indexes on nodes and relationships (Community)

Deliberately NOT used:

* node key constraints and property existence constraints (Enterprise only) --
  required-field validation is enforced in the service layer instead.
"""

from __future__ import annotations

#: Uniqueness constraints. The composite constraint on (user_id, normalized_name) is
#: what makes entity MERGE safe under concurrency: without it, two simultaneous
#: requests mentioning the same entity could create duplicate nodes.
CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT memory_id_unique IF NOT EXISTS "
    "FOR (m:Memory) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT source_id_unique IF NOT EXISTS "
    "FOR (s:Source) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT entity_identity_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE (e.user_id, e.normalized_name) IS UNIQUE",
)

#: Lookup indexes. Every query filters on user_id, so it leads each composite index.
INDEXES: tuple[str, ...] = (
    "CREATE INDEX memory_user_status IF NOT EXISTS "
    "FOR (m:Memory) ON (m.user_id, m.status)",
    "CREATE INDEX memory_user_created IF NOT EXISTS "
    "FOR (m:Memory) ON (m.user_id, m.created_at)",
    "CREATE INDEX memory_user_type IF NOT EXISTS "
    "FOR (m:Memory) ON (m.user_id, m.memory_type)",
    "CREATE INDEX entity_user_type IF NOT EXISTS "
    "FOR (e:Entity) ON (e.user_id, e.entity_type)",
    "CREATE INDEX source_user IF NOT EXISTS "
    "FOR (s:Source) ON (s.user_id)",
    "CREATE INDEX relates_to_user_status IF NOT EXISTS "
    "FOR ()-[r:RELATES_TO]-() ON (r.user_id, r.status)",
    "CREATE INDEX relates_to_predicate IF NOT EXISTS "
    "FOR ()-[r:RELATES_TO]-() ON (r.predicate)",
    # PHASE4_PLAN.md section 14: the dense-graph benchmark measured
    # GET_MEMORY's `OPTIONAL MATCH (...)-[r:RELATES_TO {..., memory_id:
    # $memory_id}]-(...)`  -- every memory read's relationship lookup --
    # doing an unindexed scan of every RELATES_TO edge for the user before
    # this index existed: ~425ms per call at 219,782 edges, versus ~2ms on a
    # sparse graph. This was the actual bottleneck behind hybrid retrieval's
    # dense-graph latency, not the multi-hop traversal itself (which stayed
    # under 1.5s at the same scale) -- adding this index fixed it directly,
    # which is why no fan-out cap was added instead: measurement showed a
    # missing index was the real cause, not too many candidates.
    "CREATE INDEX relates_to_memory_id IF NOT EXISTS "
    "FOR ()-[r:RELATES_TO]-() ON (r.memory_id)",
)

SCHEMA_STATEMENTS: tuple[str, ...] = CONSTRAINTS + INDEXES

SHOW_CONSTRAINTS = "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties"
SHOW_INDEXES = "SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state"
