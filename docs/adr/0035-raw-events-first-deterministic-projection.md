# ADR 0035 — Raw events first, deterministic idempotent projection (Phase 16)

**Status:** Accepted

## Decision
Agent events and Git commits are recorded in PostgreSQL first (idempotent, auditable, ordered). Canonical memory is a
*projection*: through the existing governance pipeline for agent claims (`apply_structured_candidate`, no LLM), and
directly with deterministic ids for immutable Git/parser evidence. Memory ids are `uuid5(space, source id[, part])`;
`MemoryService.create` gained an optional `memory_id` (default unchanged) because its `id_fn` also generates entity ids.
Projection locks the row, adopts an existing memory, and is retried by the sweeper in session order.

## Consequences
Replays, concurrent projection and crash-after-write converge on one memory. Neo4j outages never lose events
(eventual consistency). Agent-stated conflicts are stored `conflicting`, not enqueued for human review (limitation).
