# Phase 4 Plan — Graph Intelligence + Memory Governance

**Status:** Proposed. Awaiting approval. No application code has been written for
this phase.

Design only, per the approved process. Four areas, as scoped: multi-hop graph
retrieval, memory/entity/conflict review workflow, evaluation-driven threshold
calibration, and retrieval robustness/scale evaluation. No agent, no answer
generation, no document ingestion.

---

## 0. What Phase 4 extends, not replaces

```text
message → PostgreSQL → gate → job → LLM → grounding → candidate
    → decision engine (exact-triple, then semantic evidence) → Neo4j → provenance
```
```text
query → HybridRetrievalService → vector + graph(1-hop) + keyword → temporal
    filter (Neo4j-verified) → linear rank → RetrievalExplanation
```

Phase 4 extends the graph channel from 1-hop to bounded multi-hop with decay and
path capture (§2-5), adds a **new** governance layer on top of the existing
uncertain states (`LIKELY_DUPLICATE`, `FLAGGED`, `CONFLICTING`,
`entity_merge_candidates`) that today have no structured way to be listed,
inspected, or acted on (§6-11), and evaluates — rather than guesses — the
thresholds and ranking weights already introduced in Phase 3 (§12-19). It
changes no exact-triple logic, no conservative policy, no `LLMClient`/
`EmbeddingClient` abstraction, and does not touch extraction at all except to
make one existing gap (§6) structurally queryable.

**Kept unchanged, explicitly:** Neo4j as sole canonical memory; PostgreSQL for
application/workflow state only; exact-triple-first decision engine (ADR 0007);
semantic similarity as evidence, never authority (ADR 0009); append-only
temporal history (ADR 0002); the linear, explainable ranking formula (no learned
reranker); `FakeLLMClient`/`FakeEmbeddingClient` as the default, no-API-key test
posture; Railway/Vercel/Aura, no Docker, no Kubernetes.

---

## 1. Multi-hop graph retrieval algorithm

Today's `_graph_candidates` (`app/services/hybrid_retrieval_service.py`) does
exactly two things: substring-match the query against every entity name
(`RetrievalService.list_all_entities`, bounded 500), then fetch **that entity's
own memories only** (`get_entity_memories`, hop 0 relative to the query mention).
It never calls `RetrievalService.get_neighbourhood` at all, despite that method
already existing from Phase 1 with hop-bounded traversal and per-node distance
(`GraphNode.hops`).

**New algorithm**, replacing the body of `_graph_candidates` (same public
contract: `dict[memory_id, float]`, now sourced from a richer internal shape):

```text
1. Find mentioned entities (unchanged: substring match against entity names).
2. For each mentioned entity, call get_neighbourhood(entity_id, hops=RETRIEVAL_GRAPH_MAX_HOPS)
   -> Neighborhood { nodes: [GraphNode{entity, hops}], relationships: [...] }
3. For every node in the neighbourhood (the root entity is hops=0; others 1..N),
   fetch that entity's own memories (get_entity_memories, bounded, same as today).
4. A memory reachable via multiple nodes/entities takes the MINIMUM hop distance
   across all of them (mirrors get_neighbourhood's own min-length dedup).
5. Score = graph_decay(hop) (see §2). Record the path (see §4) that produced the
   minimum-hop discovery.
```

This is a genuine extension of existing, already-tested code
(`get_neighbourhood`), not a new traversal primitive — `EntityRepository`'s
`NEIGHBOURHOOD_NODES_TEMPLATE` and `NEIGHBOURHOOD_EDGES` queries are reused
as-is. What is new: (a) fetching memories per neighbourhood node rather than
just the root, (b) the decay function, (c) path capture (§4, which needs a
**new** Cypher query — the existing one only returns min-hop-distance nodes, not
the path that achieved it).

## 2. Graph decay formula

```python
def graph_relevance(hops: int, *, decay_rate: float) -> float:
    """hops=0 (direct mention, today's only case) -> 1.0, always.
    hops>=1 -> decay_rate ** hops.
    """
    if hops <= 0:
        return 1.0
    return decay_rate ** hops
```

Exponential, not linear, for one concrete reason: a linear formula
(`1.0 - hops * step`) requires a floor check to avoid going negative past some
hop count and produces an arbitrary discontinuity at that floor; exponential
decay is monotonically decreasing and bounded in `(0, 1]` for any `hops >= 0`
with no special-casing, and composes naturally with the other `[0,1]`-ranged
signals in the ranking formula. `decay_rate` is the one evaluated parameter
(`RETRIEVAL_GRAPH_DECAY`, proposed default `0.5`, meaning hop 1 contributes half
of hop 0's relevance, hop 2 a quarter) — **not asserted as correct**; §19 measures
sensitivity to it using the expanded dataset before any default is treated as
final. `hops=0` always returning exactly `1.0` is what makes this change
backward-compatible with every existing Phase 3 test that asserts a direct
entity mention scores `1.0`.

## 3. Traversal limits

| Limit | Default | Where enforced |
|---|---|---|
| `RETRIEVAL_GRAPH_MAX_HOPS` | 2 | New setting; validated the same way `validate_hops` already validates against `GRAPH_MAX_HOPS` — reject before any query is built, never truncate silently |
| Hard ceiling | 3 | `RETRIEVAL_GRAPH_MAX_HOPS` itself is capped `le=3` at the `Settings` field level — retrieval-triggered traversal is deliberately more conservative than the existing user-driven `GET /entities/{id}/neighbourhood` endpoint's own cap (`GRAPH_MAX_HOPS`, default 4), because retrieval traversal is automatic and per-query, not an explicit, single user action |
| Max neighbourhood nodes per mentioned entity | `_ENTITY_MENTION_SCAN_LIMIT`-scale bound, proposed 200 | New query-level `LIMIT` on the neighbourhood query, distinct from the existing unbounded-within-hops behavior of `get_neighbourhood` (that method is used interactively today, one entity at a time; retrieval may call it for several mentioned entities per query, so a tighter per-call bound is warranted) |
| Max total graph candidates contributed to ranking | `RETRIEVAL_GRAPH_MAX_CANDIDATES`, proposed 100 | Applied after §1 step 3, before scoring — bounds ranking-stage cost regardless of how many nodes/memories were found |
| Cycle handling | Free (see below) | No extra code needed |

**Cycles need no special handling.** A shortest path in an unweighted graph
never revisits a node — if it did, removing the revisited segment would yield a
strictly shorter path, contradicting "shortest." Since scoring is keyed on the
*minimum* hop distance (§1 step 4, matching `get_neighbourhood`'s existing
`min(length(path))` aggregation), every node's contributing path is implicitly
shortest, and cycles simply never appear in it. This is an important
simplification: no visited-set bookkeeping is needed in application code.

**Duplicate node/path handling** is the same aggregation: `min(length(path))`
per node already collapses multiple paths to one distance; §4 additionally
requires capturing *which* shortest path was used (Cypher's `ORDER BY length
... WITH ... collect(path)[0]` pattern — order is preserved into `collect`).

## 4. Path representation

```python
@dataclass(frozen=True)
class GraphPathStep:
    entity_id: str
    entity_name: str
    predicate: str | None   # the relationship into this step; None for the root
    direction: str | None   # "outgoing" | "incoming"; None for the root

@dataclass(frozen=True)
class GraphPath:
    steps: list[GraphPathStep]   # root first, target last
    hops: int                    # == len(steps) - 1
```

Populated from a **new** Cypher query (the existing `NEIGHBOURHOOD_NODES_TEMPLATE`
discards path shape after computing `min(length(path))`):

```cypher
MATCH (root:Entity {id: $entity_id, user_id: $user_id})
MATCH path = (root)-[rels:RELATES_TO*1..{hops}]-(other:Entity {user_id: $user_id})
WHERE all(r IN rels WHERE r.user_id = $user_id AND NOT r.status IN $excluded_statuses)
  AND all(n IN nodes(path) WHERE n.user_id = $user_id)
  AND other.id <> $entity_id
WITH other, path, length(path) AS len
ORDER BY len ASC
WITH other, collect(path)[0] AS shortest_path, min(len) AS hops
RETURN other {.*} AS entity, hops, shortest_path
LIMIT $max_nodes
```

`shortest_path`'s nodes/relationships are unpacked in Python into
`GraphPathStep`s (predicate + direction per hop, entity id/name per node) — no
natural-language generation, exactly as instructed. This *replaces*
`NEIGHBOURHOOD_NODES_TEMPLATE` for the retrieval call site (same shape, now
carrying the path too); the existing template stays unchanged for the
interactive `GET /entities/{id}/neighbourhood` endpoint, which doesn't need
per-node paths today and shouldn't pay the extra `collect(path)` cost for no
consumer.

`RetrievalExplanation` gains, only when the graph channel found the result:

```python
graph_distance: int | None    # hops; None if not found via graph
graph_path: list[GraphPathStep] | None
```

Never fabricated: both are `None` unless `_graph_candidates` actually populated
them for that memory (PHASE3_PLAN.md's existing rule for this dataclass,
unchanged).

## 5. Preventing graph explosion

The real risk, named correctly in the brief: `MATCH path = (root)-[*1..N]-(other)`
enumerates **every** path up to length `N`, not just shortest ones, before the
`ORDER BY`/`collect` step reduces them — on a hub node (one `Technology` entity
connected to hundreds of `Project` entities), a 2-hop query from any one project
must traverse through that hub, and the path count between the hub's neighbors
is `O(degree²)`. At degree 500 that is 250,000 paths for *one* mentioned entity's
2-hop expansion.

Protections, layered (matching the project's standing "bounded, not unbounded"
discipline):

1. **Hops capped at 3, defaulting to 2** (§3) — the smallest change with the
   largest cost impact, since path count grows combinatorially with hop count.
2. **A query-level `LIMIT`** (§4's `$max_nodes`) bounds the *output* size, but
   Neo4j's planner may still need to compute paths before the limit applies
   depending on the chosen plan — this is exactly why §17's dense-graph
   benchmark exists: to measure whether `LIMIT` alone is sufficient or whether a
   further protection is needed.
3. **A candidate, not yet committed, protection: fan-out capping.** If §17
   shows real explosion on hub nodes, the mitigation is a `WHERE` clause
   excluding expansion through nodes above a configurable degree
   (`size((n)--()) <= $max_fanout`), applied only to *intermediate* nodes (hop 1
   in a 2-hop query), never the root or the final target — a hub can still be
   *found*, it just isn't used as a stepping-stone to reach a third node through
   it. **This is proposed but not committed**: implement the simple version
   (hops + LIMIT) first, measure on the dense-graph synthetic dataset (§17),
   and add fan-out capping only if the measurement demonstrates the need — per
   the explicit instruction not to assume the current tiny evaluation graph
   represents realistic cost, and equally not to add a mitigation without
   evidence it's needed.
4. **User scoping** is already structural (every clause filters `user_id`) —
   unaffected by this change, unaudited-worthy alteration.
5. **A per-request traversal time budget** is *not* proposed as a new
   mechanism — the existing `NEO4J_MAX_TRANSACTION_RETRY_TIME` fails a hung
   transaction fast already; a dedicated per-query timeout is worth adding only
   if §17 shows the existing guards are insufficient.

## 6. Memory review data model

**The concrete gap this closes**: a `LIKELY_DUPLICATE`/`FLAGGED` decision today
is recorded only as unstructured text in `extraction_candidates.decision_reason`
(a Phase 3 report limitation, restated in the Phase 3 approval). There is no
queryable "list of pending reviews." `CONFLICTING` fares slightly better — the
graph has a real `CONFLICTS_WITH` edge — but there is still no PostgreSQL-side
"has this been looked at yet" state.

**New table**, mirroring `entity_merge_candidates`'s existing shape exactly
(same discipline: opaque Neo4j pointers, JSONB evidence, a status enum,
never canonical content):

```python
class MemoryReviewCandidate(Base):
    __tablename__ = "memory_review_candidates"

    id: str                          # uuid, pk
    user_id: str                     # FK users.id
    kind: str                        # 'likely_duplicate' | 'flagged' | 'conflicting'
    memory_id_a: str                 # opaque Neo4j Memory.id (the new/candidate memory)
    memory_id_b: str                 # opaque Neo4j Memory.id (the existing match)
    source_extraction_candidate_id: str | None  # FK extraction_candidates.id --
                                                 # the "LLM proposed -> engine
                                                 # decided" link this table extends
    evidence: dict                   # JSONB: similarity, shared entities, predicate, etc.
    score: float | None              # cosine similarity, when semantic evidence produced this row
    status: str                      # see §7 state machine
    version: int                     # optimistic concurrency (§15)
    created_at: datetime
    updated_at: datetime

    __table_args__ = (
        UniqueConstraint("user_id", "memory_id_a", "memory_id_b", "kind"),
        CheckConstraint("kind IN ('likely_duplicate','flagged','conflicting')"),
        CheckConstraint("memory_id_a <> memory_id_b"),
    )
```

Populated by the extraction runner at the exact point it already computes a
`LIKELY_DUPLICATE`/`FLAGGED`/`CONFLICTING` decision (`_apply_candidate` in
`app/extraction/runner.py`) — one additional, small, structured insert
alongside the existing `decision_reason` text, not a new decision path.
`CONFLICTS_WITH` in Neo4j is unchanged and remains the canonical record that a
conflict exists; this table is the PostgreSQL-side "has anyone looked at it"
workflow state layered on top (§13's boundary decision).

`entity_merge_candidates` gains one column: `version: int` (§15) — no other
change; its 3-state status enum (`unreviewed`/`merged`/`rejected`) is kept, not
widened (see §7 for why `confirmed_distinct` is *not* added to it).

## 7. Review state machine

**`memory_review_candidates.status`**: `unreviewed` → `confirmed_duplicate` |
`confirmed_distinct` | `conflict_resolved`. All three are terminal — no
transition back to `unreviewed` (a correction is a *new*, explicit reversal
action recorded in the audit log, §11, never a state reset, so the history of
"what was decided and when" is never overwritten in place — ADR 0002's
append-only principle, applied to review state).

**`entity_merge_candidates.status`**: kept exactly as Phase 2 defined it —
`unreviewed` → `merged` | `rejected`. `confirmed_distinct` is deliberately
**not** added here: `rejected` already means exactly that ("reviewed, not a
match"), and widening this enum would be change for its own sake. A merge can
additionally move `merged` → `merged` again only via the idempotency guard
(§16); true reversal (§9) is a separate, explicit action, not a status value.

```text
memory_review_candidate:  unreviewed ──┬─→ confirmed_duplicate   (terminal)
                                        ├─→ confirmed_distinct    (terminal)
                                        └─→ conflict_resolved     (terminal, kind='conflicting' only)

entity_merge_candidate:   unreviewed ──┬─→ merged                (terminal, reversible via §9)
                                        └─→ rejected              (terminal)
```

## 8. Duplicate-confirmation semantics

**Never delete.** `CONFIRM_DUPLICATE` on a `memory_review_candidate` (memory A =
the newer/candidate memory, memory B = the existing match):

1. New Neo4j relationship type: `(A)-[:DUPLICATE_OF]->(B)`. `MERGE`, not
   `CREATE` (idempotent — §16).
2. A gains a **new** `MemoryStatus.MERGED` (added to the enum; see below for why
   `SUPERSEDED` is the wrong fit). `SUPERSEDE_RELATIONSHIPS`'s exact pattern is
   reused to close A's own relationship edges with the same status.
3. B is untouched — it remains (or becomes, if it wasn't already) the canonical
   representative. `GET_MEMORY` gains `duplicate_of` (outgoing `DUPLICATE_OF`,
   mirrors `supersedes`) and a reverse `duplicates` (incoming, mirrors
   `superseded_by`) field.
4. Nothing is deleted: A's node, its `Source`s, its `extraction_candidates` row
   (timestamps, model/prompt version, the original `decision_reason`), and B's
   own history are all exactly as they were. Retrieval excludes `MERGED` from
   default results (added to `EXCLUDED_FROM_DEFAULT_RETRIEVAL`, alongside
   `INVALID`/`ARCHIVED`) but `GET /memories/{A}` still resolves A directly,
   showing `duplicate_of: [B]`.

**Why a new status rather than reusing `SUPERSEDED`:** `SUPERSEDED` means "this
fact changed over time" (ADR 0003) — a temporal claim. `MERGED` means "this is
the same fact as another, consolidated for retrieval" — not a temporal claim at
all; A and B could have the exact same `valid_from`. Conflating the two would
make a temporal query ("what did I believe in March") silently absorb
duplicate-consolidation semantics it was never designed for. `CONFIRM_DISTINCT`
makes no graph change at all — it only transitions the PostgreSQL row (a human
looked at it and confirmed the engine's caution was warranted; nothing to
undo in Neo4j since nothing was ever merged).

## 9. Entity-merge semantics

The highest-risk part of this plan. Requirements from the brief, addressed in
order:

**Never automatic solely from embedding similarity** — unchanged from Phase 3:
`entity_merge_candidates` rows are proposals; `MERGE_ENTITY` is always an
explicit, separate reviewer action.

**Preserve aliases, provenance, avoid dangling relationships, redirect rather
than delete:**

```text
MERGE_ENTITY(canonical=A, duplicate=B, merge_id=<new uuid>):
  1. B.name is unioned into A.aliases (exactly MERGE_ENTITIES' existing
     ON MATCH alias-union logic, applied once, explicitly, here).
  2. For every relationship (B)-[r]->(x) or (x)-[r]->(B):
       CREATE an equivalent (A)-[r']->(x) / (x)-[r']->(A), copying every
       property of r (predicate, status, confidence, memory_id, valid_from,
       valid_until) and tagging r'.merged_via = merge_id.
     r itself is left untouched -- not deleted, not mutated -- so B's original
     assertions remain exactly as extracted, still reachable by querying B
     directly.
  3. (B)-[:MERGED_INTO {merge_id: merge_id}]->(A) is created (MERGE, idempotent).
  4. B is NEVER deleted. It becomes a redirect target.
```

**Redirect resolution**: every entity-lookup path that can land on `B`
(`find_entity_by_name`, `get_entity`, the graph-retrieval entity-mention scan)
must, after finding `B`, check for an outgoing `MERGED_INTO` edge and follow it
to `A` transparently — one extra `OPTIONAL MATCH` per lookup, not a new query
shape. This is the mechanism that makes "preserve previous entity IDs... where
necessary" concrete: any stored reference to `B`'s id (a Postgres
`entity_merge_candidates.entity_id_a/b`, an old extraction candidate's raw
payload) still resolves, through the redirect, to current canonical state.

**Cross-user merge prevention**: structural, not a new check — both `A` and `B`
must resolve via the same `$user_id`-scoped lookup before the merge Cypher runs
at all; a cross-user pair simply cannot both match `{user_id: $user_id}` in the
same statement.

**Idempotency**: `MERGE (B)-[:MERGED_INTO {merge_id: ...}]->(A)` — but a *second*
`MERGE_ENTITY(A, B)` call must not re-copy B's relationships a second time.
Guard: before step 2, check whether `(B)-[:MERGED_INTO]->(A)` already exists; if
so, the whole operation is a no-op (return the existing `merge_id`), matching
the "submitting the same action twice must not corrupt memory" requirement
(§23).

## 10. Merge-reversal strategy

Every edge created by a merge is tagged `merged_via: <merge_id>` (§9 step 2) and
the redirect edge carries the same `merge_id` — this is what makes reversal
precise rather than a guess:

```text
REVERSE_MERGE(merge_id):
  1. DELETE every (A)-[r' {merged_via: merge_id}]->(x) / (x)-[r' {merged_via: merge_id}]->(A)
     -- exactly and only the edges this specific merge created.
  2. DELETE the (B)-[:MERGED_INTO {merge_id: merge_id}]->(A) edge.
  3. B's alias addition is NOT automatically retracted (B.aliases may have
     accumulated other, unrelated aliases since; blindly removing one string is
     not safe). This is a stated, accepted limitation: reversing a merge undoes
     the structural consolidation completely, but a manually-added alias
     relationship is left for a reviewer to separately clean up if wrong. B's
     original relationships (never touched by the merge, §9 step 2) require no
     action -- they were never modified.
  4. A review_audit_log row records the reversal, referencing the original
     MERGE_ENTITY audit entry -- the full history (merge, then reversal) is
     preserved, never erased.
```

Only valid while `entity_merge_candidates.status = 'merged'`; reversing moves it
to a new terminal status `reversed` (one addition to that enum, justified by
this being a genuinely new, previously-impossible outcome — unlike
`confirmed_distinct` in §7, which duplicated an existing meaning).

## 11. Conflict-resolution semantics

Three outcomes, all reusing existing, already-tested Neo4j mutations under a new
human trigger rather than the extraction engine:

| Outcome | Mechanism | Reuses |
|---|---|---|
| One fact replaced the other | `SUPERSEDE_MEMORIES` + `SUPERSEDE_RELATIONSHIPS`, run against the two conflicting memories with the reviewer naming which is newer | Exact Phase 1/2 Cypher, unchanged |
| Both genuinely concurrent (`KEEP_BOTH`) | The existing `CONFLICTS_WITH` edge gains `reviewed: true`, `resolution: "keep_both"`, `reviewed_at`, `reviewed_by` properties -- never removed, so the historical fact "these were once flagged as conflicting" survives | `LINK_CONFLICTING`'s edge, extended with properties only |
| One assertion was simply wrong | `INVALIDATE_MEMORY` + `INVALIDATE_RELATIONSHIPS` on the rejected one | Exact Phase 1 Cypher, unchanged |

No new Neo4j relationship type for conflicts. `memory_review_candidates` (kind=
`conflicting`) status moves to `conflict_resolved` regardless of which of the
three outcomes was chosen; *which* outcome is recorded in `review_audit_log`
(§11 below), not as a separate status value — the row's job is "has this been
looked at," not "what exactly was decided" (that distinction belongs to the
audit trail, which can express arbitrarily more detail without enum changes).

## 11a. Review audit trail

```python
class ReviewAuditLog(Base):
    __tablename__ = "review_audit_log"

    id: str
    user_id: str
    action: str              # CONFIRM_DUPLICATE | CONFIRM_DISTINCT | MERGE_ENTITY |
                              # REJECT_ENTITY_MERGE | REVERSE_MERGE |
                              # RESOLVE_CONFLICT_SUPERSEDE | RESOLVE_CONFLICT_KEEP_BOTH |
                              # RESOLVE_CONFLICT_REJECT
    target_type: str         # 'memory_review_candidate' | 'entity_merge_candidate'
    target_id: str           # FK into whichever table
    previous_state: dict      # JSONB snapshot (status, version, relevant graph ids)
    resulting_state: dict     # JSONB snapshot
    reason: str | None
    reviewer_id: str          # = user_id today; a distinct reviewer identity is Phase 8
    created_at: datetime
```

Insert-only, never updated (matches the project's whole append-only ethos).
Distinguishes exactly the three-stage provenance the brief asks for: "LLM
proposed" is `extraction_candidates.payload`/`decision`; "engine decided" is
`extraction_candidates.decision`/`decision_reason` (unchanged, never
overwritten); "human corrected" is this table, referencing the original
candidate via `memory_review_candidates.source_extraction_candidate_id` — three
distinguishable records, never merged into one.

## 12-13. PostgreSQL/Neo4j boundary

Confirmed, not changed: **PostgreSQL holds review workflow/audit state only**
(`memory_review_candidates`, `review_audit_log`, `entity_merge_candidates` +
its new `version` column) — every id it stores is an opaque pointer, exactly
`extraction_candidates`' existing discipline. **Neo4j holds every canonical
consequence of a review decision** — new relationship types
(`DUPLICATE_OF`, `MERGED_INTO`), the new `MERGED` status, `CONFLICTS_WITH`'s new
properties, alias unions. Nothing added here duplicates graph content into
PostgreSQL; the `evidence`/`previous_state`/`resulting_state` JSONB columns
store small snapshots (ids, scores, status strings) for audit purposes, not
memory or entity content.

## 14. Cross-database consistency strategy

A review action writes to **both** stores. No distributed transaction (ADR
0006's standing rule, unchanged). Order, matching Phase 2/3's established
pattern exactly: **Neo4j mutation first** (it's canonical), **PostgreSQL
status/audit write second**. Both mutations are idempotent (§9's `merged_via`
tagging, `MERGE` throughout, §16) so a retry after a partial failure converges
rather than duplicating effects.

**Crash windows, explicitly**:

| Case | State left behind | Recovery |
|---|---|---|
| Neo4j mutated, process dies before PostgreSQL write | Graph reflects the decision; `memory_review_candidates`/`entity_merge_candidates` row still shows `unreviewed` | A client retry of the *same* action finds the graph already in the target state (idempotent check, e.g. `DUPLICATE_OF` already exists) and only needs to complete the PostgreSQL write -- reruns safely to completion |
| PostgreSQL updated, Neo4j write never attempted | Cannot happen by construction -- Neo4j is always attempted first in the same request; a request that never reaches the Postgres write also never reached this state |
| Reconciliation | A new bounded sweep tick (mirroring `run_sweep_tick`'s shape), run periodically: for every `unreviewed` review candidate, check whether the graph already reflects a resolution (`DUPLICATE_OF`/`MERGED_INTO`/a reviewed `CONFLICTS_WITH`) and self-heal the PostgreSQL row if so -- the same "sweep catches what the live path missed" idiom already established for extraction (ADR 0008), applied here instead of inventing a new mechanism |

## 15. Concurrency strategy

Optimistic concurrency via the new `version: int` column (both
`memory_review_candidates` and `entity_merge_candidates`). Every `POST
/review/.../{id}/...` action requires `if_version` matching the row's current
`version`; a mismatch returns `409 Conflict` (existing `ConflictError`, no new
error type needed) naming the current version, exactly the `if_version` pattern
already used by the Artifact database tooling and, in spirit, by
`ExtractionCandidateRepository`'s idempotency checks. "Reviewer A loads,
Reviewer B resolves, Reviewer A submits stale" is closed structurally: A's
stale `if_version` fails against B's already-incremented row.

## 16. Idempotency strategy

- **Neo4j**: every new relationship (`DUPLICATE_OF`, `MERGED_INTO`) created via
  `MERGE`, never `CREATE` — re-running the identical mutation is a no-op.
- **PostgreSQL**: the review endpoint checks current `status` before writing; if
  it already equals the requested terminal status (same action re-submitted
  before any other change, same `if_version`), the request returns the existing
  state without a second `review_audit_log` insert or `version` bump —
  distinguished from a genuine *conflicting* second call (different action, or
  stale `if_version`) by whether the resulting state would actually differ.
- **Tested explicitly** (§23): repeated `CONFIRM_DUPLICATE`, repeated
  `MERGE_ENTITY`, repeated conflict resolution, repeated `KEEP_BOTH` each
  produce exactly one graph mutation and exactly one audit row, regardless of
  how many times the identical request is sent.

## 17. Threshold-calibration methodology

New labeled dataset, `eval/datasets/threshold_calibration_v1.jsonl`, covering
exactly the categories named in the brief (true duplicates, paraphrases,
related-but-distinct facts, conflicts, unrelated facts, ambiguous entities, true
aliases, false entity matches) — each row a pair plus a ground-truth label, not
just a query/expected-result shape like `retrieval_v1.jsonl`. Every pair's
`FakeEmbeddingClient` cosine similarity is computed and recorded *during
dataset construction* (the Phase 3 methodology, carried forward: verify the
fake embedding actually produces the intended similarity before trusting a
scenario to exercise what it claims to).

**Sweep**: thresholds from the data's own similarity distribution (not a fixed
0.05 grid blindly applied) — compute all pairwise similarities in the labeled
set first, then sweep at each distinct value (or a bounded number of quantiles
if the set is large) so no threshold that would matter is skipped between grid
points. At each threshold, report precision, recall, F1, false-positive rate,
false-negative rate, computed against the labels — separately for **memory**
duplicate detection and **entity** similarity (§18 explains why these are kept
distinct).

**Conservative objective, not max-F1** (explicit per the brief): the report
presents a trade-off table --

```text
threshold  precision  recall  F1   false_positive_rate  false_negative_rate
0.99       1.00       0.31    0.47  0.00                 0.69
0.90       0.94       0.58    0.72  0.02                 0.42
0.80       0.81       0.79    0.80  0.09                 0.21
...
```
(illustrative shape only -- §17's actual numbers come from a real run, never
invented) -- and a recommendation is stated in terms of the project's own
policy: prefer the threshold with the **lowest false-positive rate among those
clearing a stated minimum recall**, not the highest F1. The current Phase 3
defaults (0.90 duplicate / 0.80 flag / 0.85 entity) are either confirmed or
revised **based on this table**, shown to the user before any default changes.

## 18. Separate thresholds — investigated, not assumed

Today: `SEMANTIC_DUPLICATE_THRESHOLD`/`SEMANTIC_FLAG_THRESHOLD` (memory content)
and `SEMANTIC_ENTITY_FLAG_THRESHOLD` (entity names) are *already* separate
settings (Phase 3) — the brief's question is whether more granularity is
needed. Investigation finding, stated directly: **there is currently no
semantic threshold for conflicts at all** — `classify_relationship`'s
CONFLICTING outcome is 100% exact-triple (same predicate, different target, no
change signal); semantic evidence (`app/semantic/similarity.py`) never produces
`CONFLICTING`, only `LIKELY_DUPLICATE`/`FLAGGED`/`NEW`. Adding a *new*
semantic-conflict classifier is **not proposed** for Phase 4: the calibration
dataset (§17) includes conflict-shaped pairs specifically to measure whether the
existing exact-triple detection plus human review (§11) already handles them
adequately before any new automated signal is considered — consistent with "do
not add infrastructure/algorithms without a demonstrated need." If §17's data
shows real conflicts going undetected because they're phrased differently
rather than structurally exact, that becomes a documented, evidence-based Phase
5+ candidate, not something guessed into existence here.

Memory-duplicate and entity-similarity thresholds are kept as separate settings
(confirmed correct by design, not merely inherited) because they measure
different things: entity names are short, low-information strings where a
coincidental word match is more likely and more dangerous (auto-flagging
`"Apple"`/`"Apple Records"`, the brief's own example) than the same coincidence
in a full memory statement.

## 19. Retrieval evaluation expansion

`eval/datasets/retrieval_v2.jsonl` replaces the 4-scenario `retrieval_v1.jsonl`
with an expanded set (target: 40-60 scenarios) spanning every category named in
the brief: direct fact, semantic paraphrase, entity-centric, **multi-hop**
(constructed 2-3-hop chains, e.g. `Project --uses--> Technology --category-->
Category`, queried by a question that only a traversal can answer), current-state,
historical, conflicting-memory, sparse graph, dense graph (a hub entity with
many neighbours — shared with §21's dense-graph test), irrelevant distractors,
similar-but-wrong memories, and cross-user isolation embedded directly in the
dataset (a scenario whose "expected relevant" set is deliberately empty when
queried as a different synthetic user). Every scenario's gold `memory_id`s are
real ids from an actual pipeline run (never hand-typed), following
`eval/runner.py`'s and `retrieval_v1.jsonl`'s established methodology, and every
similarity-dependent scenario has its `FakeEmbeddingClient` cosine verified
during construction (§17's same discipline).

## 20. Scale-oriented synthetic evaluation

New `eval/generate_synthetic_dataset.py`: a **deterministic** generator (fixed
seed) producing memories/entities at configurable sizes --- **100, 1,000,
10,000**, explicitly the largest "practical locally" per the instruction, no
larger. Two topology modes: uniform-random (baseline) and hub-heavy (one or a
few high-degree entities, feeding §21). Measures, at each size: graph query
latency (1-hop and multi-hop separately), vector query latency, keyword query
latency, hybrid end-to-end latency, multi-hop traversal latency specifically,
candidate counts per stage, and ranking latency in isolation -- mean/median/p95/max
over repeated runs, the same honest-numbers discipline as the Phase 3
benchmarks (§ dataset size and environment always stated alongside every
number; no scalability claim beyond "measured on this machine, at these
sizes").

## 21. Dense-graph evaluation

A dedicated scenario generated by the hub-heavy mode: one `Technology` entity
connected (via `uses`) to a configurable number of `Project` entities (tested at
50, 200, 1000 neighbours). Multi-hop retrieval queries anchored at one project
must remain bounded per §3/§5's protections -- this is the concrete test of
whether "hops + LIMIT" (§5's shipped protection) is sufficient or whether
fan-out capping (§5's evaluated-not-committed protection) is actually needed.
Measures path count actually returned, query latency, and whether latency
degrades linearly or worse as hub degree grows -- the deciding evidence for
§5's open question.

## 22. Retrieval ablations (expanded)

Extends Phase 3's 3-configuration ablation (`graph_only`/`vector_only`/`hybrid`,
via the existing `enable_vector`/`enable_graph`/`enable_keyword` testing seam on
`HybridRetrievalService`) to the full matrix named in the brief, adding two new
seams (both testing-only, no `Settings`/API surface, matching the existing
pattern's own justification):

```text
graph_hops: int = 0 | 1 | 2 | 3          # 0 = graph channel off, 1 = Phase 3 behavior
enable_graph_decay: bool                  # False => flat 1.0 for every hop (isolates
                                           #   whether decay itself helps or hurts)
enable_temporal_signal: bool              # zeroes RETRIEVAL_WEIGHT_TEMPORAL for this run
```

Configurations evaluated: `graph_1hop`, `graph_multihop`, `vector_only`,
`keyword_only`, `graph+vector`, `graph+vector+keyword` (full hybrid),
`hybrid_no_temporal`, `hybrid_no_decay`, `full_hybrid`. Run against
`retrieval_v2.jsonl` (§19), reporting Recall@K/Precision@K/MRR per
configuration per category — explicitly to test, not assume, that multi-hop
retrieval improves the queries constructed to need it, and that adding channels
never *hurts* the categories where a single channel was already sufficient
(regression protection against ranking interference).

## 23. Ranking calibration

Using §19's expanded dataset and §22's ablation infrastructure, sweep each
ranking weight (vector/graph/keyword/recency/temporal/confidence) and the new
graph-decay rate independently (holding others at their current default),
reporting how Recall@K/MRR responds. **Not** a search for new optimal weights
by default — the explicit instruction is "if the existing defaults remain
reasonable, keep them." The linear ranker itself is not replaced.

## 24. RetrievalExplanation extension

```python
@dataclass(frozen=True)
class RetrievalExplanation:
    memory_id: str
    retrieved_via: list[str]
    vector_similarity: float | None
    graph_relevance: float | None
    graph_distance: int | None        # NEW -- hops, only when found via graph
    graph_path: list[GraphPathStep] | None  # NEW -- only when found via graph
    keyword_match: float | None
    temporal_fit: float
    confidence: float
    final_score: float
    temporal_state: str
```

`graph_relevance` becomes the *decayed* score (§2), not the flat `1.0` every
graph hit gets today — a real, visible behavior change for existing Phase 3
API consumers of `POST /search`, called out explicitly (not silently shipped)
in the implementation report when this phase ships. `graph_distance`/
`graph_path` are `None` together, always, when the graph channel didn't find
the result — never a fabricated distance for a vector- or keyword-only hit.

## 25. Review API

New router, `app/api/routes/review.py`, naming consistent with existing routers
(`extraction.py`'s public/internal split, `search.py`'s shape):

```text
GET  /review/memories                      list memory_review_candidates (paginate, filter by kind/status)
GET  /review/memories/{id}                 full detail: both memories (MemoryDetail),
                                            evidence, score, source extraction candidate,
                                            provenance for both, current retrieval explanation
                                            if either is independently searchable
POST /review/memories/{id}/confirm-duplicate   body: {if_version, reason?}
POST /review/memories/{id}/confirm-distinct    body: {if_version, reason?}
POST /review/memories/{id}/resolve-conflict    body: {if_version, outcome: supersede|keep_both|reject,
                                                       superseding_memory_id? (required if outcome=supersede),
                                                       reason?}

GET  /review/entities                      list entity_merge_candidates (paginate, filter by status)
GET  /review/entities/{id}                 both entities, evidence, score, shared/neighbourhood context
POST /review/entities/{id}/merge           body: {if_version, canonical_entity_id, reason?}
POST /review/entities/{id}/reject          body: {if_version, reason?}
POST /review/entities/{id}/reverse-merge   body: {if_version, reason?}  -- only valid if status='merged'

GET  /review/audit                         filterable by target_type/target_id/action/date range
```

All under the same `X-User-Id` header discipline as every other endpoint; every
`GET .../{id}` and `POST` verifies the candidate belongs to the requesting user
before revealing or acting on it (cross-user access reads as `404`, never
`403`, matching the codebase's standing "never confirm another user's record
exists" rule). No frontend in this phase; a minimal diagnostic HTML view is
**not** committed to now — build it only if implementation reveals genuine
difficulty testing the workflow through raw HTTP calls alone.

## 26. Review concurrency (see §15) / 27. Idempotency (see §16)

(Numbered separately in the request; addressed together above since the
mechanism — `version` + `if_version` — is the same for both properties: a
version check prevents a *stale* write, and a same-version, same-action retry
is idempotent by construction.)

## 28. Evaluation of review correctness

Deterministic scenarios, `eval/datasets/review_scenarios_v1.jsonl`, one per
category named in the brief (duplicate, false duplicate, entity alias, false
entity merge, temporal replacement, concurrent state, incorrect extraction),
each defining the expected review outcome. Run through the real pipeline
(extraction → `memory_review_candidates`/`entity_merge_candidates` populated →
simulated reviewer action → assert the resulting Neo4j/PostgreSQL state matches
§8-11's designed semantics exactly, including that history/provenance survived
). This is what proves §8's "never simply delete" and §9's "never dangling
relationships" are actually true, not just designed.

## 29. Security

Every review endpoint is user-scoped exactly like every other endpoint in the
application — no new mechanism, the same `$user_id`/`user_id` discipline
audited by the existing static Cypher and SQL safety tests (both audits require
no changes to their scope rules; new repository files fall under the existing
`app/repositories/` glob automatically). Explicit regression test, mirroring
the Phase 3 pattern exactly: User A's `FLAGGED` memory pair, User B attempts
every review action against it by id → `404` for all of them, and no merge or
conflict-resolution mutation reaches Neo4j (verified by checking graph state
is unchanged after the attempt, not just by checking the HTTP status).

## 30. Deployment impact

None. No new infrastructure category, no new managed service. Same
Vercel/Railway/Aura target, no Docker, no Kubernetes — unchanged from every
prior phase.

## 31. Risks and tradeoffs

- **Entity-merge redirect resolution is the single highest-risk piece of this
  plan.** Every entity lookup gains an extra optional traversal step; the
  reversal mechanism depends on precise `merged_via` tagging discipline that
  must be gotten right the first time (a missed tag on any created edge breaks
  reversal's precision). This deserves the most implementation-time scrutiny
  and the most test coverage of anything in this phase.
- **`MemoryStatus.MERGED` is a genuine domain-model expansion**, not just an
  implementation detail — risk of confusion with `SUPERSEDED` if not documented
  clearly; §8 states the distinction explicitly for exactly this reason.
- **Dense-graph multi-hop cost is a real, not hypothetical, risk** (§5/§21) —
  mitigated by shipping the conservative version first and measuring before
  adding fan-out capping, rather than guessing a cap now that might be either
  too strict (missing real answers) or too loose (not actually protective).
- **`graph_relevance` changing from flat `1.0` to decayed** is a visible
  behavior change to existing Phase 3 API consumers of `/search` — flagged
  explicitly (§24) so it is never a silent regression in the final report.
- **Optimistic concurrency adds client-side bookkeeping** (tracking `version`
  per candidate) — the proportionate, standard answer to the stated race
  condition, not overhead added without cause.
- **No frontend still means the review workflow is only as good as whoever
  drives it via HTTP** — acceptable for this phase's explicit scope (backend
  capability first), but worth naming as a real limitation until Phase
  5+/frontend work makes it usable day-to-day.

## 32. New dependencies

**None.** Multi-hop retrieval uses existing Cypher variable-length path syntax
(no APOC). The review workflow uses the existing FastAPI/SQLAlchemy/Alembic
stack. If §21's dense-graph measurement shows a genuine need for a
purpose-built path-finding procedure (e.g. APOC), that would be a documented,
evidence-based addition at that point — not proposed now.

## 33. Schema changes (summary)

**PostgreSQL** (one Alembic migration): new tables `memory_review_candidates`,
`review_audit_log`; new column `entity_merge_candidates.version`; enum
extensions via `CheckConstraint` updates only (`entity_merge_candidates.status`
gains `reversed`).

**Neo4j** (schemaless, additive, no migration mechanism needed): new
relationship types `DUPLICATE_OF`, `MERGED_INTO` (both carrying a `merge_id`/
provenance property where relevant); `MemoryStatus` enum (Python-side) gains
`MERGED`; `CONFLICTS_WITH` edges gain optional properties
(`reviewed`, `resolution`, `reviewed_at`, `reviewed_by`) — no new node labels,
no change to `scripts/init_graph.py`'s constraints unless §21's measurement
motivates a new index (evaluated, not committed).

## 34. API changes (summary)

New router `app/api/routes/review.py` (§25); `RetrievalExplanation` gains two
fields (§24); `GET_MEMORY`'s Cypher gains `duplicate_of`/`duplicates` fields
(§8); no existing endpoint's request shape changes (only response shape, for
`/search` and `/memories/{id}`, both additive).

## 35. Exact implementation order

Review-workflow foundations are independent of multi-hop retrieval and are
ordered first — lower implementation risk, and unlocks §28's correctness
evaluation earlier. This ordering is a judgment call, not a hard dependency;
multi-hop (steps 10-13) could run in parallel or first if preferred.

```text
1.  PostgreSQL foundation: migration for memory_review_candidates,
    review_audit_log, entity_merge_candidates.version
2.  Neo4j repository methods: MemoryStatus.MERGED, DUPLICATE_OF/MERGED_INTO
    Cypher (pure repository layer, unit-testable against fakes first)
3.  Structured candidate persistence: extraction runner writes
    memory_review_candidates rows for LIKELY_DUPLICATE/FLAGGED/CONFLICTING
    (closes the Phase 3 gap)
4.  Review read APIs: GET /review/memories, /review/entities (+ detail),
    no mutations yet -- fully testable in isolation
5.  Duplicate-confirmation write path + audit log
6.  Conflict-resolution write path (all three outcomes)
7.  Entity-merge write path (isolated, highest scrutiny)
8.  Entity-merge reversal
9.  Optimistic concurrency (version/if_version) across every write endpoint
10. Idempotency tests for every write action (§28's repeated-submission cases)
11. Reconciliation sweep for the cross-database consistency window (§14)
12. Multi-hop graph retrieval: path-capturing Cypher, GraphPath model,
    decay function, wiring into _graph_candidates
13. RetrievalExplanation extension (graph_distance/graph_path)
14. Graph-explosion protections + dense-graph synthetic benchmark (§21) --
    decides whether fan-out capping (§5) ships in this phase or is deferred
15. Threshold-calibration dataset + sweep (§17-18)
16. Expanded retrieval dataset (§19) + scale-oriented synthetic benchmarks
    (§20) + expanded ablations (§22) + ranking-weight sensitivity (§23)
17. Review-correctness evaluation scenarios (§28)
18. Security regression tests (§29)
19. Hardening pass (mirroring docs/PHASE2_HARDENING.md's shape) + final report
```

---

## Summary: what this plan does and does not authorize

**Authorized once approved:** sections 1-35 and the implementation order above,
with the same verification discipline as every prior phase — real Neo4j +
PostgreSQL testing throughout, no fabricated evaluation numbers, a hardening
pass before final sign-off.

**Not authorized, and not part of this plan:** any agent loop, tool-calling,
autonomous planning, answer generation, LangGraph/MCP, external web search,
document ingestion, a learned reranker, a new automated semantic-conflict
classifier (§18), fan-out capping without measured evidence (§5/§21), or any
new infrastructure category.

Waiting for approval before writing any Phase 4 application code.
