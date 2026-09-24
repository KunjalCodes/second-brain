# ADR 0009 — pgvector for embeddings; Neo4j remains canonical memory

**Status:** Proposed (Phase 3 — not yet implemented; part of `docs/PHASE3_PLAN.md`)
**Extends:** [ADR 0006](0006-postgresql-for-application-state.md)

## Context

Phase 2's exact-triple duplicate/conflict detection cannot recognize that "uses
Gemini for classification" and "classifies with Gemini" describe the same fact —
this was accepted at the time as a stated, deliberate limitation motivating
Phase 3 (`docs/PHASE2_PLAN.md`), never a workaround. Closing it requires semantic
similarity, which requires embeddings and a place to search them.

ADR 0006 already anticipated this moment explicitly: "`pgvector` is NOT enabled.
Vector retrieval is Phase 3, and installing the extension early would invite
premature use." This ADR is that decision, now that it is time to make it.

Two questions need answering: (1) where do embeddings live, and (2) what is the
consistency model between wherever that is and Neo4j.

## Decision

**Enable `pgvector` on the existing managed PostgreSQL. Neo4j remains the sole
canonical memory store, unchanged.** A new table, `memory_embeddings`, holds one
row per (memory, embedding provider, embedding model), referencing the canonical
Neo4j `Memory.id` by an opaque string — exactly the pattern
`extraction_candidates.memory_id` already established in ADR 0006, extended to a
new data type rather than reinvented.

### The boundary rule, restated for this new data

> **A `memory_embeddings` row is a disposable, regenerable derivative of a
> canonical Neo4j Memory. It is never the other way around.**

Concretely:

- A memory's `content`, `status`, `valid_from`/`valid_until`, entities and
  relationships are never copied into PostgreSQL. `memory_embeddings` stores only
  the vector and the metadata needed to know which model produced it
  (`embedding_provider`, `embedding_model`, `content_hash`).
- Embedding generation happens **after** a memory is already durably persisted in
  Neo4j, asynchronously, and can be regenerated at any time from the Neo4j
  content it was derived from. Losing every row in `memory_embeddings` loses
  *retrieval convenience*, never *memory*.
- Retrieval correctness for anything Neo4j already tracks authoritatively —
  status, temporal validity — is decided by reading Neo4j at query time, never by
  trusting a cached flag in PostgreSQL. This is the direct answer to "a similarity
  search must never become a side-channel that bypasses Neo4j's existing
  isolation [or temporal] guarantees": it structurally cannot, because
  `memory_embeddings` does not carry the fields that would let it disagree with
  Neo4j about those questions in the first place — it does not store them at all.

### Why pgvector-in-existing-Postgres, not a dedicated vector database

1. **One fewer managed service**, matching ARCHITECTURE.md §1/§24's standing
   preference for simple infrastructure and every prior phase's "no new
   infrastructure category without a demonstrated reason." Nothing in the current
   or projected data volume (a personal/portfolio-scale memory graph, not a
   multi-tenant SaaS at scale) demonstrates that need yet.
2. **The same isolation and safety discipline already applies for free.**
   `memory_embeddings` is a `UserScopedRepository`-governed table like every
   other Phase 2 table, covered by the existing static SQL-isolation audit with
   no new tooling. A separate vector database would need its own, new isolation
   enforcement built from scratch.
3. **Consistency model stays a single, already-understood shape.** ADR 0006
   already established "PostgreSQL and Neo4j cannot share one transaction; data
   flows one way; design for at-least-once." Adding a *third* store would mean a
   *second*, different two-store consistency problem to reason about, rather than
   one more table inside the boundary already drawn.
4. **pgvector is a standard, non-exotic extension** available on every managed
   Postgres host the deployment target already names (Supabase, Neon, Railway's
   own Postgres) — no new hosting relationship required.

## Consequences

- One new PostgreSQL extension (`vector`) and one new table, added via Alembic
  migration exactly like every other Phase 2 schema change (ARCHITECTURE.md §27).
- **At-least-once, asynchronous consistency**, the same shape ADR 0006 already
  accepted for extraction: an embedding can lag behind its memory's creation, or
  briefly not exist, and this must never be observable as "the memory does not
  exist" — only as "semantic retrieval doesn't have this one yet." A bounded,
  resumable backfill sweep (mirroring `run_sweep_tick`) is what reconciles the
  lag, not a distributed transaction.
- **Embeddings are versioned by `(provider, model)`, never overwritten in
  place.** A model change produces new rows alongside old ones; retrieval always
  reads the currently-configured model's rows. This trades some storage growth
  for the ability to migrate models without a destructive, all-or-nothing
  cutover — consistent with the project's standing preference for never silently
  discarding recoverable state.
- **Semantic similarity is evidence, not a decision-maker.** This ADR covers
  *where embeddings live and how they stay consistent with Neo4j* — it does not
  by itself authorize automatic merging or overwriting based on similarity.
  That policy question is already settled by ADR 0007 ("the LLM proposes, the
  application decides") and `docs/PHASE3_PLAN.md` §5 extends it explicitly:
  similarity crosses a configurable threshold into a `LIKELY_DUPLICATE`/`FLAGGED`
  *signal for review*, never a merge.
- HNSW indexing is chosen over IVFFlat specifically because the table starts
  small/empty and IVFFlat's training-set requirement is actively counterproductive
  at that stage (`docs/PHASE3_PLAN.md` §15) — an implementation detail recorded
  here only because it follows directly from this ADR's "correctness and low
  operational overhead first" framing, not because it is itself a separate
  architectural decision.
- Phase 1/2 code is unchanged. `MemoryService`/`MemoryRepository` gain a new,
  independent caller (the embedding pipeline) exactly the way Phase 2 added
  callers without rewriting Phase 1, per ADR 0006's closing consequence.
