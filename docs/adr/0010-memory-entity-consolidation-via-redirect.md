# ADR 0010 — Confirmed duplicates and entity merges consolidate via redirect relationships, never deletion; review workflow state lives in PostgreSQL

**Status:** Accepted (Phase 4 — implemented)
**Extends:** [ADR 0002](0002-bitemporal-append-only.md) (append-only history),
[ADR 0006](0006-postgresql-for-application-state.md) (PostgreSQL/Neo4j boundary),
[ADR 0007](0007-llm-proposes-application-decides.md) (conservative policy)

## Context

Phase 3 introduced semantic evidence that can flag two memories as a likely
duplicate, or two entities as a likely alias pair — but a flag is not a
decision, and Phase 3 explicitly never merges anything automatically. Phase 4
adds the human review step that can *confirm* a duplicate or a merge is
correct. Two questions need a real answer before any code is written: what
does "confirmed duplicate" actually do to the graph (a decision with real,
hard-to-reverse consequences if gotten wrong), and where does the fact that a
review happened get recorded.

## Decision

**A confirmed duplicate or entity merge is represented as an additional graph
relationship pointing from the redundant record to the canonical one. Neither
record is ever deleted.** Concretely:

- `(memory_B)-[:DUPLICATE_OF]->(memory_A)` — B keeps its node, its `Source`s,
  its `extraction_candidates` history, its timestamps, exactly as before. B's
  status becomes `MERGED` (excluded from default retrieval, like `INVALID`/
  `ARCHIVED`, but semantically distinct from `SUPERSEDED` — see
  `docs/PHASE4_PLAN.md` section 8 for why the two must not be conflated).
- `(entity_B)-[:MERGED_INTO {merge_id}]->(entity_A)` — B's relationships are
  never deleted or moved; equivalent copies are created from A, each tagged
  `merged_via: <merge_id>`, so a merge is fully, precisely reversible by
  deleting exactly the edges that one merge created. Every entity lookup that
  can land on B follows the redirect to A transparently.

**Review workflow state — has this candidate been looked at, what was decided,
by whom, when — lives in PostgreSQL**, in new tables (`memory_review_candidates`,
`review_audit_log`) that reference Neo4j records only by opaque id, exactly the
pattern `extraction_candidates` already established. **The canonical
consequence of a decision — the new relationship, the status change, the
alias — lives in Neo4j**, unconditionally, matching every prior phase's
boundary rule.

## Rationale

**Why redirect, not delete.** The single clearest instruction in this
project's history is "never destroy historical evidence" (CLAUDE.md section 5,
restated for every phase since). A human reviewer confirming two things are
duplicates is still a claim, made with the same tools (semantic similarity, an
LLM's prior extraction) that already produce false positives often enough to
require review in the first place. Deleting B on a reviewer's word makes an
incorrect confirmation unrecoverable; redirecting makes it a one-step
reversal (ADR 0010's `merged_via` tagging exists specifically to make that
reversal precise rather than a manual audit of the whole graph).

**Why a new `MERGED` status rather than reusing `SUPERSEDED`.** `SUPERSEDED`
is a temporal claim — this fact was true, then it changed (ADR 0003).
"Duplicate" is not a temporal claim at all: two memories can assert the exact
same fact with the exact same validity window, extracted from two different
messages. Reusing `SUPERSEDED` would make a historical/`as_of` query
inadvertently absorb duplicate-consolidation semantics never designed into it.

**Why PostgreSQL for review state, unconditionally.** This is not a new
argument — it is ADR 0006's argument, applied to one more kind of application
state. Review status is workflow metadata about *when a human looked at
something*, not a fact about the world the memory graph represents. Keeping it
in PostgreSQL keeps Neo4j's write surface exactly as small as ADR 0006 already
committed to, and gives the review workflow the same transactional/claim
primitives (row versions, `SELECT ... FOR UPDATE`-style concurrency) that
extraction's job queue already relies on, for the same reasons.

## Consequences

- Two new Neo4j relationship types (`DUPLICATE_OF`, `MERGED_INTO`) and one new
  `MemoryStatus` value (`MERGED`) — additive, no migration needed for Neo4j's
  schemaless model, but a real, permanent expansion of the domain model that
  every future retrieval/API consumer must know not to confuse with
  `SUPERSEDED`.
- `RetrievalService.get_entity()`, `find_entity_by_name()`, and (closed as a
  follow-up fix after initial Phase 4 review) `HybridRetrievalService`'s
  graph-channel entity-mention scan all resolve through redirects before
  using an entity, via the single shared `RetrievalService.resolve_entity()`
  pass-through to `EntityRepository.resolve` — never a second
  reimplementation. A lookup, a name search, or a query mention of an old,
  merged-away id/name all transparently resolve to the canonical entity's
  current state (`old/merged entity name -> mention detection -> canonical
  resolution -> retrieval`), including redirect chains and cyclic-redirect
  safety. This is a small, permanent extra Neo4j round trip per lookup or
  per mentioned entity, accepted as the price of true reversibility. The
  merged-away entity's own node and any memory that mentions only it (never
  touched by a merge, which only copies `RELATES_TO` edges) remain fully
  intact and directly resolvable by id — they are simply no longer what the
  graph channel anchors on once a redirect exists, consistent with
  `get_entity`/`find_entity_by_name`'s existing behaviour.
- `entity_merge_candidates` and the new `memory_review_candidates` gain a
  `version` column for optimistic concurrency (ADR-level consequence: this is
  the first PostgreSQL table in the project needing conflict-on-write
  semantics beyond simple idempotent upserts, since review decisions — unlike
  extraction candidates — can legitimately be contested by two humans at once).
- A confirmed merge is reversible; a confirmed duplicate consolidation is not
  designed with an explicit un-confirm action in this phase (the record is
  never destroyed, so a human can always inspect and manually correct it later,
  but no dedicated "un-merge a duplicate" endpoint ships now — only entity
  merges, the riskier and more structurally invasive operation, get one).
- No cross-database transaction is introduced. The consistency model is
  identical in kind to ADR 0006/0008's: Neo4j mutates first (canonical), 
  PostgreSQL records the review outcome second, both idempotent, a
  reconciliation sweep closes the gap if a crash lands between the two.
