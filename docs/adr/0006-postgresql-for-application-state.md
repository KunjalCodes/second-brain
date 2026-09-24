# ADR 0006 — PostgreSQL for application state; Neo4j remains canonical memory

**Status:** Accepted (Phase 2)
**Supersedes:** the Phase-1-only scope of [ADR 0004](0004-neo4j-only-no-docker.md)

## Context

[ADR 0004](0004-neo4j-only-no-docker.md) made Neo4j the *only* datastore in Phase 1,
and the Phase 0 review had argued for deferring a second store as long as possible.
That was correct while the only data was the memory graph itself.

Phase 2 introduces conversations and messages: a linear, append-only, high-volume log
queried by `(conversation_id, sequence)`, plus extraction job state that needs
transactional claim/retry semantics. Neither is graph-shaped data.

The question is whether to keep messages in Neo4j
(`(:Conversation)-[:HAS_MESSAGE]->(:Message)`) or introduce PostgreSQL now.

## Decision

**Introduce managed PostgreSQL in Phase 2, with a strict responsibility split.**

### PostgreSQL — application and conversation state

Users/identity references, conversations, messages and message metadata, extraction
jobs and their status, idempotency keys, application metadata.

### Neo4j — canonical persistent AI memory

`Entity`, `Memory`, `Source`, relationships, temporal relationship state, provenance
relationships, graph traversal, memory history, current/historical projections.

### The boundary rule

> **PostgreSQL must never become a second source of truth for memory.**

The canonical memory graph is **not** duplicated, mirrored, cached or projected into
PostgreSQL. PostgreSQL stores *raw input and processing state*; Neo4j stores
*derived knowledge*. Data flows one way:

```text
PostgreSQL ──(conversation / message / source IDs)──► Extraction pipeline ──► Neo4j
```

No reverse reference exists: nothing in PostgreSQL stores a memory's content,
status, validity window, entities or relationships. The single exception is
`extraction_candidates.memory_id`, which is an opaque outbound pointer used for
idempotency and provenance display, never a copy of memory state.

## Rationale

**Rework avoidance, not capability.** Neo4j *can* hold messages. The argument is
about what changing later would cost.

1. **Phase 3 requires PostgreSQL regardless**, for embeddings. Message embeddings
   attach to the message row. Keeping messages in Neo4j through Phase 2 means Phase 3
   migrates the message log *and* rewrites every provenance reference — one
   infrastructure addition becomes two plus a data migration.
2. **Modelling.** A message list is a list. Storing it as nodes because a graph
   database happens to be available is modelling by accident.
3. **Aura's free tier is 200k nodes / 400k relationships.** A thousand 50-message
   conversations consumes roughly a quarter of that on chat logs, crowding out the
   knowledge the graph exists to hold.
4. **Job state needs relational primitives.** Safe at-least-once claiming uses
   `SELECT ... FOR UPDATE SKIP LOCKED`, and idempotency uses a unique constraint.
   Both are native in PostgreSQL and awkward in Cypher.

**This does not recreate the dual-write problem ADR 0004 avoided.** There, the risk
was *the same entity written to two stores*, with no cross-store transaction to keep
them consistent. Here the two stores hold *different* entities joined by a
one-directional reference to immutable rows:

* A message is committed to PostgreSQL **before** extraction begins, so a memory's
  provenance reference can never dangle.
* A message whose extraction failed is simply *not yet extracted* — a normal,
  recoverable state recorded in `extraction_jobs`, not corruption.
* Memory writes remain a single Neo4j transaction, exactly as in Phase 1.

## Consequences

* A second managed service (Supabase or Neon), a second connection pool, and a
  second check in `/health/ready`.
* Alembic migrations arrive, satisfying `ARCHITECTURE.md` section 27. Migrations run
  as a **pre-deploy release step**, never at application startup — concurrent
  instances must not race to migrate.
* **`pgvector` is NOT enabled.** Vector retrieval is Phase 3, and installing the
  extension early would invite premature use.
* Only portable SQL and standard PostgreSQL features are used, so the same schema
  runs on a local Homebrew PostgreSQL, Supabase or Neon with only `DATABASE_URL`
  changing.
* **User isolation now spans two stores.** The same `user_id` string is the
  isolation key in both: a PostgreSQL column with a foreign key, and the Neo4j
  namespace property. Both require independent enforcement and independent tests.
* Phase 1 code is unchanged. `MemoryService` gains a second caller, not a rewrite.
