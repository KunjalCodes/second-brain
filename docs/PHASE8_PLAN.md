# Phase 8 — Memory Graph Exploration API

**Status:** Proposed, awaiting approval. Planning only — no application code,
tests, dependencies, migrations, database state, Neo4j state, frontend, or
other documentation were changed in producing this document.

**Baseline inspected:** commit `2fcd507d3e59ae69b80da4065fc5d1d06b489936`
(Phase 7 checkpoint), branch `master`, clean working tree, 769 tests passing,
local Neo4j `2026.08.1` Community (Cypher 5/25), local PostgreSQL.

---

## 1. Executive Summary

Phase 8 should establish a **bounded, read-only, temporally-aware,
provenance-linked graph exploration API** over the canonical Neo4j memory
graph: the backend contract a future Memory Explorer UI will consume.

This is a backend-only phase. It builds no frontend, changes no
authentication, adds no dependency, changes no PostgreSQL schema, and adds no
Neo4j labels, relationship types, or constraints.

It was selected over authentication (the roadmap's original Phase 8) and over
a frontend-first phase because direct inspection shows that the graph
*primitives* exist (Phases 1/4) but the *contract* an exploration client needs
does not:

- The only interactive graph endpoint, `GET /entities/{id}/graph`, is
  structurally unbounded in result size. Its node query
  (`NEIGHBOURHOOD_NODES_TEMPLATE`) has no `LIMIT`, and its hop bound defaults
  to 4 (configurable to 10). Its dense-graph behaviour has never been
  measured: the Phase 4 benchmark measured only the separately-capped
  retrieval path (`neighbourhood_with_paths`).
- No graph endpoint supports temporal slicing (`as_of`). Superseded and
  current edges come back mixed together. A client would have to reimplement
  the half-open validity semantics that `lifecycle.py` and
  `get_entity_state` already own.
- Graph responses contain only `Entity` nodes and `RELATES_TO` edges. The
  canonical graph's `Memory` and `Source` nodes, and the
  `MENTIONS`/`DERIVED_FROM`/`SUPERSEDES`/`CONFLICTS_WITH`/`DUPLICATE_OF`/
  `MERGED_INTO` edges, are not representable in any graph payload.
- Entities merged away in the review workflow still appear as first-class
  `/entities/search` hits. Redirect information is resolved and then
  discarded.
- Getting from one relationship to its supporting memories and their sources
  takes 1 + 2k HTTP round trips for k asserting memories.
- There is no bounded "landing view". A client cannot show "my graph" without
  already knowing an entity id or typing a search term.

Every one of these is fixable with a read-only projection layer that reuses
existing services. All of it can be verified **deterministically, with no LLM
calls**, which means Phase 8 is not blocked by the Gemini free-tier
rate-limiting that constrained all real-LLM evaluation in Phases 6 and 7.

Authentication stays the required gate before any non-local exposure. It is
recommended as the next phase (§24).

---

## 2. Current System State

What follows was verified against the code, not taken from the roadmap.

### 2.1 Implemented capabilities (Phases 1–7)

| Area | State | Evidence |
|---|---|---|
| Canonical memory graph | Neo4j: `Memory`, `Entity`, `Source` nodes; `MENTIONS` (Memory→Entity), `RELATES_TO` (Entity→Entity, carries `memory_id`, `predicate`, `status`, `valid_from`/`valid_until`), `DERIVED_FROM` (Memory→Source), `SUPERSEDES`, `CONFLICTS_WITH`, `DUPLICATE_OF` (Memory→Memory), `MERGED_INTO` (Entity→Entity) | `app/repositories/{memory,entity}_repository.py`, `app/graph/schema.py` |
| Temporal model | Bi-temporal, half-open `[valid_from, valid_until)`, append-only; status transition table | `app/models/lifecycle.py`, ADR 0002/0003 |
| Extraction | LLM proposes, deterministic engine decides (NEW/DUPLICATE/UPDATE/SUPERSEDING/CONFLICTING/LIKELY_DUPLICATE/FLAGGED/INVALID/IGNORE); conversations and document chunks share one pipeline | `app/extraction/`, ADR 0007/0011/0012 |
| Entity resolution | Normalization + aliases + canonical-key + embedding *flags* (never auto-merge); human-reviewed merges via `MERGED_INTO` redirect, reversible | `app/extraction/resolution.py`, `EntityRepository.resolve/merge/reverse_merge`, ADR 0010 |
| Retrieval | Hybrid vector (pgvector) + bounded graph (≤3 hops, ≤2000 nodes, default 200) + keyword, explicit linear rerank, deterministic temporal intent | `HybridRetrievalService`, ADR 0009 |
| Agent | Custom loop, 7 bounded tools (6 read + 1 gated write), structural citation validation | `AgentService`, `app/agent/tools.py`, ADR 0011 |
| Documents | TXT/MD/text-layer PDF → deterministic chunks → same extraction pipeline; provenance to document/chunk/page | `app/documents/`, ADR 0012–0014 |
| Review workflow | Memory duplicate/conflict review, entity merge approve/reject/reverse, audit log, cross-DB reconciliation — HTTP-only | `app/api/routes/review.py`, ADR 0010 |
| Evaluation | Six harnesses; persistent, resumable runs (`evaluation_runs`, `evaluation_task_results`); `retrieval_necessity`; evaluation-only baseline B | `backend/eval/`, ADR 0015–0017 |
| Tests | 769 passing (549 unit / 220 integration); `ruff` clean | Phase 7 report |

### 2.2 What is not implemented

| Area | State | Evidence |
|---|---|---|
| Authentication | None. `X-User-Id` header placeholder (falls back to `DEV_USER_ID`); startup refuses `APP_ENV=production` with header trust enabled. `AUTH_SECRET` appears in `.env.example` but no `Settings` field exists. | ADR 0005 ("temporary; replaced in Phase 8"), `app/api/deps.py`, `app/core/config.py` |
| Rate limiting | None | `app/main.py`, `app/api/deps.py` |
| Frontend | Two files (`frontend/README.md`, `frontend/.env.example`), zero application code | `frontend/` |
| Observability | stdlib `logging` only | `app/core/logging.py` |
| Deployment | No deploy workflow; CI is backend-only (lint, unit, safety audits; integration tests weekly/manual) | `.github/workflows/` |
| LLM providers | `fake`, `gemini` only | `app/llm/factory.py` |

### 2.3 Readiness of the backend for a real Memory Explorer

This answers the questions in the Phase 8 brief (§10), each against the code.

| Question | Answer | Evidence |
|---|---|---|
| Can the frontend retrieve a user's graph safely? | **Partially.** Isolation is correct (every node and edge filtered on `$user_id`), but identity is unauthenticated (ADR 0005). Retrieval is root-centric only, with no landing slice. | `NEIGHBOURHOOD_NODES_TEMPLATE`; `list_all_entities` exists but is not routed |
| Can it retrieve entities and relationships? | **Yes.** | `GET /entities/{id}`, `GET /entities/{id}/relationships` |
| Can it expand a node by one or more hops? | **Yes, but unbounded in size.** No `LIMIT`, no node or edge budget, no truncation signal, no "already loaded" frontier, and the whole neighbourhood is re-fetched on each expansion. Hops default max is 4, configurable to 10. | `entity_repository.py` `NEIGHBOURHOOD_NODES_TEMPLATE`; `config.py` `graph_max_hops` |
| Can it retrieve the memories supporting a relationship? | **Partially, via N+1.** Each `RELATES_TO` edge carries one `memory_id`. Separate memories asserting the same triple are separate edges (ADR 0001) and are not aggregated. | `Relationship.memory_id`; `GET /memories/{id}` |
| Can it retrieve provenance? | **Yes, per memory.** Resolves to conversation messages and to document/chunk/page. | `GET /memories/{id}/provenance`, `ProvenanceService` |
| Can it inspect temporal history? | **Partially.** Entity-level current/historical split at an instant exists. The memory supersession/duplicate chain exists only as an agent tool, not over REST. **No graph-level `as_of`.** | `GET /entities/{id}/history`; `tools.get_memory_history` |
| Can merged entities be represented correctly? | **Partially.** A merged-away id resolves to the canonical entity, but `original_id`/`redirect_depth` are discarded. **Merged-away entities appear in `/entities/search`.** `MERGED_INTO` appears in no payload. | `RetrievalService.get_entity`; `SEARCH_ENTITIES` has no `MERGED_INTO` filter; no test covers search after merge |
| Can superseded memories be represented? | **Per memory, yes.** `/memories/{id}` exposes `supersedes`/`superseded_by`. **Not in any graph payload** — no memory nodes. | `MemoryDetail` |
| Can large graphs be paginated or bounded? | **No, for the interactive endpoint.** Only the retrieval and agent paths are capped. | `neighbourhood()` vs `neighbourhood_with_paths(max_nodes=...)` |
| Can traversal avoid leaking another user's data? | **Yes, at the query level.** Every node in every path and every edge is user-filtered. Caveat: the static audit only covers two explicitly listed modules. | `tests/unit/test_cypher_safety.py` parametrized on `[memory_repository, entity_repository]` |
| Can the API avoid exposing raw Cypher? | **Yes.** Only the validated integer hop bound is interpolated. | `validate_hops`, `test_only_the_hop_bound_is_interpolated` |
| Can the UI request slices rather than the whole database? | **Only root-centric neighbourhoods,** with no filters by type, predicate, status, or time, and no budget. | `GET /entities/{id}/graph` has only `hops` |

**Verdict:** the backend is not ready for a real Memory Explorer. What's
missing is not graph data or isolation. It is a bounded, sliceable,
temporally-correct, provenance-linked read contract.

### 2.4 Documentation inaccuracy found

`docs/API.md` (≈ line 937) says `RETRIEVAL_GRAPH_MAX_NODES` "bounds a single
neighbourhood traversal's result size". In code it bounds only the retrieval
traversal (`neighbourhood_with_paths`), not `GET /entities/{id}/graph`. It is
recorded here and corrected during implementation (§18, Stage 8). No
documentation is changed in this planning step.

---

## 3. Problem Being Solved

The project's thesis is a persistent memory layer whose knowledge is
inspectable (CLAUDE.md §24: "AI memory should be inspectable"; §23: "the graph
should never be merely decorative"). The eventual Memory Explorer must show
the **real** canonical graph (entities, relationships, the memories asserting
them, and their sources), not a demo projection.

Today, any client that tried would have to do one of two things. It could
make unbounded requests, where a hub entity at 2–4 hops can return an
arbitrarily large neighbourhood (unmeasured, structurally uncapped). Or it
could reimplement server-side semantics client-side:

- half-open temporal filtering,
- current-vs-superseded classification,
- merged-entity resolution,
- aggregation of multiple edges for one triple,
- the supersession-chain walk.

That second option is exactly the "disconnected visualization layer" the
brief warns against. Temporal and identity semantics would live in two places
and drift.

The problem Phase 8 solves: **make the canonical graph explorable through a
bounded, server-authoritative contract**, so every semantic decision
(temporal, identity, provenance, isolation, budget) stays on the server where
it is already implemented and tested.

---

## 4. Why This Phase Comes Next

1. **It closes a latent robustness gap that exists today, independent of any
   UI.** An unbounded, unmeasured read endpoint is a performance hazard for
   any API consumer. Phase 4's own discipline ("measure, don't assume") was
   applied to the retrieval path but never to this endpoint.
2. **It is the hard prerequisite for the Memory Explorer.** Building a UI
   first would force the UI to encode temporal and identity semantics, or to
   ship against endpoints that will change. Building the contract first gives
   the UI phase a stable, tested target.
3. **It is orthogonal to authentication.** ADR 0005 deliberately confined
   identity to one dependency (`get_current_user_id`). Every endpoint built
   on `CurrentUser` inherits real authentication automatically when it
   lands. Sequencing the graph API first loses nothing. Sequencing
   authentication first secures an exploration surface that doesn't exist
   yet, before anything is deployed or has a login-capable client.
4. **It is fully verifiable within the zero-cost constraint and immune to the
   Phase 7 limitation.** Phase 6 and Phase 7 real-LLM evaluation was blocked
   by free-tier rate limits (agent 0/6, documents 0/5 in Phase 7). Graph-API
   correctness involves no LLM, so Phase 8 can be completely validated —
   "Tested > Claimed" — regardless of quota.
5. **It reuses Phase 7's infrastructure as intended.** Phase 7 wired
   `EvaluationRunTracker` only into real-LLM sections and noted deterministic
   historization as a natural extension. Phase 8's conformance harness is
   its first deterministic consumer.
6. **It is bounded.** Read-only, no schema change, no new dependency, no
   agent or retrieval change. It fits one phase without becoming a
   multi-phase project.

This deviates from the README roadmap (Phase 8 = authentication +
productionization). The deviation is deliberate and is the main decision for
approval. See §6.3 and §24.

---

## 5. Candidate Capabilities Considered

### 5.1 Serious candidates: evidence matrix

The four serious candidates:

- **A:** Memory Graph Exploration API (selected)
- **B:** Authentication and multi-user productionization (roadmap's Phase 8)
- **C:** Local/open-source LLM provider for real-LLM evaluation resilience
- **D:** Memory Explorer frontend now

| # | Criterion | A. Graph Exploration API | B. Auth + productionization | C. Local LLM provider | D. Explorer frontend now |
|---|---|---|---|---|---|
| 1 | Why needed now | Interactive graph endpoint unbounded/unmeasured; no temporal or memory-level graph contract (§2.3) | ADR 0005: backend must not face untrusted callers until auth exists | Real-LLM eval blocked by quota in Phases 6–7 | Product needs a visual explorer eventually |
| 2 | Limitation addressed | Unbounded `/graph`; no `as_of`; merged-away search hits; N+1 provenance; no landing slice | Header-placeholder identity; no rate limiting | 0/6 and 0/5 real-LLM coverage in Phase 7 | No UI exists |
| 3 | Existing architecture | `RetrievalService`, `EntityRepository` (neighbourhood, resolve), `MemoryService`, `ProvenanceService`, temporal Cypher precedent (memory search `as_of`), Cypher audit | `get_current_user_id` single seam; startup guard | `LLMClient` protocol + factory; `complete_structured` | None: two files |
| 4 | New architecture | Graph projection schema, service, repository templates, router | User store, password hashing, token issuance/verification, session/refresh, rate limiting | New provider adapter; schema-compat layer (Phase 5 found three Gemini schema bugs; a new provider can reproduce that class) | Entire Next.js app, graph rendering, state, API client |
| 5 | PostgreSQL impact | None expected | New users/credentials/sessions tables | None | None |
| 6 | Neo4j impact | Read-only; no labels/types/constraints; one index only if measurement demands (§8.2) | None | None | None |
| 7 | API impact | New `/graph/*` read endpoints; two decision-gated fixes to existing endpoints | Login/token endpoints; every route's identity source changes | None | Consumes whatever exists (today: insufficient) |
| 8 | Agent impact | None (tools already bounded) | None | Could change which model runs the agent (configurable) | None |
| 9 | Retrieval impact | None | None | None | None |
| 10 | Evaluation impact | Deterministic conformance harness via Phase 7 tracker | Auth/isolation tests | Produces real-LLM evidence, but for a different model than configured | UI testing needs tooling not present |
| 11 | Security / isolation | Largest per-request data aggregation of any endpoint, so user scoping and budgets must be airtight; still unauthenticated (dev-only) | Directly strengthens isolation | Neutral (local) | Browser client + unauthenticated header = any user's graph readable if exposed |
| 12 | Provenance | Strengthens: one-call edge → memory → source | Neutral | Neutral | Depends on A |
| 13 | Temporal | Strengthens: server-side `as_of` slicing, never client-reimplemented | Neutral | Neutral | Would reimplement temporal logic client-side without A |
| 14 | Testing | Fully deterministic; real Neo4j/PostgreSQL integration; no LLM | Deterministic; security-critical | Needs local model runtime; hardware-dependent | Needs frontend test stack |
| 15 | Zero-cost | Yes: local Neo4j Community, existing libs | Yes if self-hosted JWT + OSS hashing (one new dependency likely, e.g. an argon2/bcrypt binding) | Yes (e.g. Ollama, OSS) but hardware-bound; quality uncertain | Yes (OSS stack) |
| 16 | Scope | Contained, read-only | Medium–large; security-critical | Small–medium, open-ended quality risk | Large; starting from zero |
| 17 | Unlocks future phases | Unlocks Explorer UI with a stable contract; independent of auth | Unlocks deployment and any exposure | Unlocks resumable Phase 7 runs without quota | UI iteration |
| 18 | Duplicates existing work | No: extends; reuses services; extracts one existing walk rather than copying it | No | Partially overlaps the Gemini adapter pattern | Would duplicate server semantics without A |
| 19 | Feature-creep risk | Moderate (graph-query "language" temptation). Controlled by §22 | Moderate (OAuth/social/SSO temptation) | Moderate (model zoo temptation) | High |
| 20 | Defer? | **No: select** | Defer to Phase 9, **before** any exposure | Defer; small standalone task | Defer until A (and ideally B) exist |

### 5.2 Candidate reasoning

**B. Authentication.** It is indispensable before exposure, and the roadmap
and ADR 0005 name it for Phase 8. But nothing is deployed, no client exists
to log in, and ADR 0005's single-seam design means auth can be added later
without touching endpoint logic. Doing it first provides no immediate
capability. Doing it second loses nothing. It must precede any non-local
deployment, and it should precede the UI if the UI will ever leave localhost
(§24).

**C. Local LLM provider.** This is the only zero-cost path to real-LLM
evidence independent of Gemini quota. It is not selected for three reasons:

- Evidence from a different model says nothing about the configured model's
  behaviour. ADR 0016's bar is about the model actually used.
- Small local models' structured-output reliability is uncertain, and
  Phase 5 already showed that provider schema compatibility is a real bug
  class.
- It would be a second consecutive evaluation-infrastructure phase.

It is recorded as a bounded standalone item (§23).

**D. Frontend now.** Rejected on evidence. There is nothing to build on,
and the backend contract it needs does not exist (§2.3). A UI built today
would either misrepresent the graph or reimplement server semantics.

### 5.3 Other candidates, briefly

- **Memory consolidation / decay / importance / lifecycle automation.** Still
  premature, for the same reasons as Phase 7 §2.2: no retrieval-usage signal
  exists, it writes canonical memory on a schedule, and there's no measured
  need. It gains an input once an explorer exists (users can *see* what is
  stale). Deferred.
- **Entity-resolution adjudication (LLM-assisted).** Gives model output
  influence over identity (ADR 0007/0010 risk), with no measured bottleneck.
  Deferred.
- **Conflict-resolution improvements / agent write governance.** No
  evidenced defect. `AGENT_WRITE_ENABLED` defaults false; ADR 0011 routes
  writes through the full pipeline. Deferred.
- **Memory review workflow / document management / user-facing memory
  management UI.** The backends exist (Phase 4 review routes, Phase 6
  document routes). Only the UI is missing, which belongs with the Explorer
  UI phase.
- **Observability.** Real, but it pairs with productionization (Phase 9).
  Phase 8 does add measurement for its own endpoints (§16).
- **Deployment / production hardening.** Requires auth first.
- **Performance / scaling (general).** No measured general need. Phase 8
  measures exactly the one unmeasured endpoint it touches.
- **Richer temporal reasoning.** Graph-level `as_of` is part of Phase 8.
  Nothing beyond that is evidenced.
- **Real-LLM evaluation resilience (resume Phase 7 runs).** No new
  capability is needed. `EvaluationRunTracker` already resumes when quota
  recovers. That is an operational action, not a phase.

---

## 6. Selected Direction

### 6.1 Objective

Provide a read-only, bounded, deterministic, user-isolated API that lets a
client:

- open a landing view of the user's graph,
- expand it incrementally,
- slice it by time, type, and predicate,
- see the memories behind any relationship,
- trace those memories to their sources,
- follow a memory's supersession, duplicate, and conflict history,

with every temporal, identity, and provenance decision computed on the server
from the existing canonical implementation.

### 6.2 Scope in one sentence

A graph *projection* layer: new schemas, service, repository templates, and
router over the unchanged canonical graph, plus two decision-gated
correctness fixes to existing endpoints and one extraction of existing logic
for reuse.

### 6.3 Decision points requiring approval

| # | Decision | Recommendation |
|---|---|---|
| D0 | Deviate from the README roadmap: Phase 8 = Graph Exploration API; authentication moves to Phase 9 | **Approve** (§4, §24). Requires README roadmap and ADR 0005 status-line updates during implementation, not now. |
| D1 | Existing `GET /entities/{id}/graph`: leave as-is, or add a server-side node cap plus an additive `truncated` field | **Decide after Stage 0 measurement.** If measured hub neighbourhoods exceed the proposed budget at realistic density, cap it. Otherwise leave it and document it as legacy/unbounded. Either way the Explorer uses the new endpoints. |
| D2 | Existing `GET /entities/search`: exclude merged-away entities by default (`include_merged=false`) | **Approve.** Returning redirected nodes as independent hits contradicts ADR 0010's "one canonical resolution mechanism." A characterization test proves current behaviour first. |
| D3 | Default temporal scope of new graph endpoints: `current` (history opt-in) | **Approve.** A landing view mixing current and superseded edges would visually assert contradictory facts. History stays one parameter away. |

---

## 7. Architecture

### 7.1 Components

```
                       FastAPI router: app/api/routes/graph.py  (new)
                                   |  (CurrentUser -- unchanged ADR 0005 seam)
                                   v
              GraphExplorationService: app/services/graph_exploration_service.py (new)
              - budget clamping, temporal-scope resolution, response assembly
              - reuses RetrievalService.resolve_entity (merge redirects)
              - reuses MemoryService.get / ProvenanceService.get_provenance (evidence)
              - reuses the memory-history walk (extracted, see 7.3)
                                   |
                                   v
           Graph projection Cypher templates (new; §7.4 for where they live)
           - user_id on every node and every edge, parameterised filters,
             only the validated hop bound interpolated, LIMIT on every read
                                   |
                                   v
                        Neo4j (canonical graph, UNCHANGED)
                                   |
             (evidence only) ProvenanceService --> PostgreSQL messages/documents
```

**Unchanged:**

- `HybridRetrievalService`, `RetrievalService`'s existing methods,
  `neighbourhood_with_paths`, all retrieval templates.
- `AgentService` and every agent tool's behaviour.
- The extraction pipeline and decision engine.
- Document ingestion.
- The review workflow.
- The evaluation framework's schema.
- Every Neo4j label, relationship type, and constraint.
- Every PostgreSQL table.

### 7.2 Data flow: incremental exploration

```
1. GET /graph/overview?limit=50                      -> landing slice (entities + edges among them)
2. user clicks node X
   GET /graph/entities/X/neighborhood?hops=1&exclude_ids=<already loaded>
                                                     -> only new nodes/edges, bounded
3. user toggles "show memories"
   ...&include=memories                              -> MENTIONS memory nodes, bounded per entity,
                                                        plus SUPERSEDES/CONFLICTS_WITH/DUPLICATE_OF among them
4. user clicks edge X -[uses]-> Y
   GET /graph/edges/evidence?source=X&target=Y&predicate=uses
                                                     -> supporting memories + resolved provenance (one call)
5. user clicks a memory M
   GET /graph/memories/M/timeline                    -> supersession/duplicate/conflict chain, chronological
6. user drags a time slider to 2026-03-15
   ...&temporal_scope=as_of&as_of=2026-03-15         -> the graph as it was then
```

### 7.3 One extraction, not a copy

The supersession/duplicate chain walk currently lives inside the agent tool
`get_memory_history` (`app/agent/tools.py`): Python-side assembly over
`MemoryService.get`, bounded by `_MAX_HISTORY_CHAIN`. The timeline endpoint
needs the same walk.

The walk moves into a service-level function used by both. The tool keeps
its own output formatting (`_compact_memory`) and its behaviour is
regression-tested unchanged. This follows the CLAUDE.md rule against
duplicating business logic.

### 7.4 Where the new Cypher lives, and closing the audit gap

`tests/unit/test_cypher_safety.py` audits only
`[memory_repository, entity_repository]`, listed explicitly. A new module
would silently escape the audit. Two options:

- **(a)** Put the projection templates in `entity_repository.py`. This is
  audited automatically but grows an already large module.
- **(b)** Put them in a new `app/repositories/graph_repository.py`, add it to
  the audit's parameter list, **and** add a meta-test: every module under
  `app/repositories/` containing Cypher-shaped constants must appear in the
  audited list.

**Recommend (b).** The meta-test closes the gap for all future modules, not
just this one. It is a test-only change that strengthens an existing
isolation guarantee.

### 7.5 Response shape: one normalized, renderer-agnostic slice

```
GraphSlice
  root:            node id | null           (null for overview)
  resolved_from:   id | null                (original id if a MERGED_INTO redirect occurred)
  redirect_depth:  int                      (0 if none)
  temporal_scope:  "current" | "as_of" | "all"
  as_of:           datetime                 (the instant temporal classification was computed at)
  budget:          {max_nodes, max_edges, max_hops, max_memories_per_entity}  (effective, post-clamp)
  truncated_nodes: bool                     (exact: fetch max+1, drop the extra)
  truncated_edges: bool
  nodes:  [ { id, kind: "entity"|"memory",
              label,                         (entity name | memory content, truncated)
              entity_type?, memory_type?, status?, confidence?,
              valid_from?, valid_until?,
              temporal_state?: "current"|"historical"|"not_yet_valid",   (server-computed)
              hops?, aliases?, merged_from_count? } ]
  edges:  [ { id, kind: "relates_to"|"mentions"|"supersedes"|"conflicts_with"|"duplicate_of",
              source, target,
              predicate?, status?, valid_from?, valid_until?, temporal_state?,
              memory_ids?: [..],             (relates_to only; bounded)
              assertion_count? } ]           (relates_to: # of RELATES_TO edges aggregated)
```

Design rules:

- **No layout coordinates.** Layout is a UI concern.
- **No totals** (counting the full neighbourhood defeats the budget); the
  truncation flags are exact.
- **`temporal_state` is always server-computed** with the same half-open
  semantics as `lifecycle.py`, so a client never reimplements them.
- **Relationship aggregation:** multiple `RELATES_TO` edges for the same
  `(source, predicate, target, temporal_state)` collapse into one slice edge
  carrying all `memory_ids` (bounded) and an `assertion_count`. The
  individual edges stay addressable through the evidence endpoint. This is a
  read-time projection of ADR 0001's "two memories, two edges" storage
  model; it never changes storage.

The shape maps directly onto node-and-edge renderers (React Flow and
Cytoscape.js, both named in the README stack) without committing to either.

### 7.6 ADRs to record during implementation (not created now)

1. **"The Memory Explorer contract is a bounded, read-only graph projection."**
   Slices, never the whole graph. Every read has a node budget, an edge
   budget, and exact truncation flags. Temporal, identity, and provenance
   semantics are server-computed. Storage is never changed to suit
   presentation.
2. **"Every repository module containing Cypher is covered by the isolation
   audit automatically."** Records the §7.4 meta-test as a standing
   guarantee.

---

## 8. Data Model

### 8.1 PostgreSQL

**No changes expected.** Evidence resolution reads existing message and
document tables through the unchanged `ProvenanceService`. The conformance
harness writes to the existing Phase 7 `evaluation_runs` /
`evaluation_task_results` tables. Nothing new is persisted: the graph API is
read-only.

### 8.2 Neo4j

**No new labels, relationship types, constraints, or properties.** Every
query reads the existing canonical structure.

**One conditional index, gated on measurement.** If Stage 0/7 profiling shows
the overview's degree ranking or a projection query performing a
per-relationship scan at 10k-node scale (the same failure shape Phase 4
found and fixed with `relates_to_memory_id`), an additive range index may be
proposed through `app/graph/schema.py` and `scripts/init_graph.py`. That
would be an explicit, approved schema step with before/after measurement,
not an assumption. None is proposed now.

The evidence endpoint is deliberately keyed by
`(source_entity_id, target_entity_id, predicate)`. That anchors on two
uniquely-constrained (hence indexed) `Entity.id` lookups instead of an
unindexed `RELATES_TO.id` lookup, so it needs no new index.

### 8.3 Configuration

New `Settings` fields, each with a hard upper bound. Values are provisional
design parameters, to be confirmed or adjusted by Stage 0 measurement, and
are not benchmark claims.

| Setting | Provisional default | Hard max | Purpose |
|---|---|---|---|
| `GRAPH_EXPLORE_MAX_NODES` | 150 | 1000 | Node budget per slice (also bounds `exclude_ids` length) |
| `GRAPH_EXPLORE_MAX_EDGES` | 400 | 3000 | Edge budget per slice |
| `GRAPH_EXPLORE_MAX_HOPS` | 2 | 3 | Aligned with retrieval's and the agent's existing ≤3 bound |
| `GRAPH_EXPLORE_MAX_MEMORIES_PER_ENTITY` | 5 | 25 | Memory-node budget when `include=memories` |

Evidence and timeline list lengths reuse the existing `search_max_limit`
clamp and `_MAX_HISTORY_CHAIN`. No new knobs are needed for those.

### 8.4 Migration strategy

None. No PostgreSQL migration and no Neo4j migration are expected. If the
conditional index (§8.2) is approved, it is applied idempotently through the
existing `init_graph.py` (`IF NOT EXISTS`) path, with no data change.

---

## 9. API / Interface Design

All endpoints are `GET`, require `CurrentUser`, return JSON, and never accept
Cypher, labels, or relationship types from the client.

List and ID parameters are validated:

- `exclude_ids` length ≤ node budget;
- IDs match the stored ID format and length;
- `predicate` values ≤ 100 chars, at most 20 values;
- `entity_type` values from the `EntityType` enum.

Offsets and limits are clamped as on existing endpoints. Cross-user IDs
return 404 (never 403), matching the existing convention.

### 9.1 `GET /graph/overview`

The bounded landing slice.

| Param | Type | Default |
|---|---|---|
| `limit` | int | `GRAPH_EXPLORE_MAX_NODES` (clamped) |
| `entity_type` | repeatable enum | all |
| `temporal_scope` | `current`\|`as_of`\|`all` | `current` |
| `as_of` | datetime | required iff `as_of` |
| `include` | repeatable: `memories`, `merges` | none |

It returns the top entities by in-scope relationship degree (ties broken by
name, then id, for determinism), merged-away entities excluded, plus the
in-scope edges among them, all within budget. If the degree ranking fails the
Stage 0 performance measurement at scale, the fallback ordering is
`updated_at DESC`. The choice is made by measurement and documented.

### 9.2 `GET /graph/entities/{entity_id}/neighborhood`

Bounded, incremental expansion.

| Param | Type | Default |
|---|---|---|
| `hops` | int 1..`GRAPH_EXPLORE_MAX_HOPS` | 1 |
| `max_nodes`, `max_edges` | int (clamped to settings) | settings |
| `temporal_scope`, `as_of` | as above | `current` |
| `predicate` | repeatable string | all |
| `entity_type` | repeatable enum | all |
| `exclude_ids` | repeatable id (≤ node budget) | none |
| `include` | `memories`, `merges` | none |

The root resolves through `RetrievalService.resolve_entity`. The response
carries `resolved_from`/`redirect_depth` when a redirect occurred. Traversal
applies the temporal and status predicate **inside** the Cypher, on every
hop. Filtering after `LIMIT` would under-fill a budget and misrepresent the
slice. `exclude_ids` nodes are neither returned nor re-expanded; edges from
new nodes back into excluded nodes are returned so the client can connect
them.

### 9.3 `GET /graph/edges/evidence`

One call from a relationship to the memories behind it and their sources.

| Param | Type |
|---|---|
| `source_entity_id`, `target_entity_id`, `predicate` | required |
| `temporal_scope`, `as_of` | as above (default `all`, since evidence inspection wants history) |
| `limit` | clamped by `search_max_limit` |

It returns each matching `RELATES_TO` edge (status, window, temporal state),
the asserting memory for each (content, status, confidence, window, link
fields), and each memory's provenance, resolved through the unchanged
`ProvenanceService`: message role and content, or document filename,
chunk content, page, and section. Content fields are length-bounded.

It also returns source nodes and `derived_from` edges, so a client can draw
Memory → Source.

This replaces today's 1 + 2k round trips with one bounded request. The
before/after request count is a measured Stage 7 result, not assumed here.

### 9.4 `GET /graph/memories/{memory_id}/timeline`

The memory's supersession, duplicate, and conflict chain, in chronological
order (`valid_from`, then `created_at`, then `id`). It is bounded by
`_MAX_HISTORY_CHAIN`, produced by the extracted walk (§7.3), and returned as
memory nodes plus `supersedes`/`duplicate_of`/`conflicts_with` edges in the
same `GraphSlice` shape.

### 9.5 Decision-gated changes to existing endpoints

- **D2 — `GET /entities/search`:** add `include_merged: bool = false`. By
  default, exclude entities with an outgoing `MERGED_INTO`. The response
  shape is unchanged.
- **D1 — `GET /entities/{id}/graph`:** only if Stage 0 justifies it, apply a
  node cap and add an optional `truncated: bool` to `Neighborhood`. This is
  an additive field that existing clients ignore.

### 9.6 Deliberately not offered

No generic graph query language, no GraphQL, no free-form filter
expressions, no `DELETE`/`PATCH` on the graph (mutation stays in the existing
memory, review, and invalidate routes), no streaming/websockets, no bulk
export.

---

## 10. Agent Impact

**None.**

- `AgentService`, the prompt, and every tool are unchanged.
- `traverse_graph` already uses the bounded `neighbourhood_with_paths`
  (≤3 hops, `retrieval_graph_max_nodes`).
- The only touch point is extracting `get_memory_history`'s walk into a
  shared function (§7.3). The tool's output must be byte-identical before
  and after, which is proven by the existing agent-tool integration tests
  and a new equality test on the tool's output for a fixed fixture.
- The Phase 7 prompt decision (unchanged, ADR 0016) is not revisited here.

---

## 11. Retrieval Impact

**None.**

- `HybridRetrievalService`, `RetrievalService`'s existing methods, and the
  `NEIGHBOURHOOD_*` templates used by retrieval are unchanged.
- New projection templates are separate constants and never alter existing
  ones.
- The regression proof: `eval/retrieval_runner.py` and the retrieval
  integration tests produce identical results before and after.
- The Explorer is a *browsing* surface. Relevance ranking remains the job of
  `POST /search`, which the Explorer can call as-is for its search box.

---

## 12. Provenance

Provenance semantics are unchanged: read through `ProvenanceService` and
never re-derived.

- Every aggregated `relates_to` edge carries the `memory_ids` that asserted
  it (ADR 0001), bounded, so any drawn edge traces to its evidence in one
  call.
- The evidence endpoint (§9.3) returns memory → source → message or
  document/chunk/page in one bounded response, reusing Phase 2 and Phase 6
  resolution.
- A corroborated memory (DUPLICATE decisions attach additional sources) shows
  all its sources.
- A deleted or unresolvable source degrades to source metadata without
  content, exactly as `ProvenanceService` already behaves. It is never an
  error, and never fabricated.
- Content returned is the requesting user's own data, length-bounded. It is
  data to render, never instructions (see §14 on rendering).

---

## 13. Temporal Semantics

Temporal meaning is computed once, on the server, with existing semantics.

**`temporal_scope=current` (default for slices).** Include an edge or memory
iff its status is in `BELIEVED_STATUSES` (`active`, `conflicting`) and its
window `[valid_from, valid_until)` contains now.

**`temporal_scope=as_of`, `as_of=T`.** Include iff its status is not in
`EXCLUDED_FROM_DEFAULT_RETRIEVAL` and its window contains `T`. A `superseded`
edge that was valid at `T` is included, because it was true then. This is the
exact semantics `get_entity_state` already uses to answer "what was I using in
March?"

**`temporal_scope=all`.** Include every non-excluded edge or memory,
regardless of window. Each is labelled with a server-computed
`temporal_state` relative to the response's `as_of` (default now):

- `current`
- `historical` (the window closed at or before `as_of`)
- `not_yet_valid` (the window starts after `as_of`)

**Invariants:**

- **Half-open boundaries** are enforced in Cypher (needed for correct
  bounding) using the same predicate form the memory-search `as_of` fragment
  already uses. They are pinned by boundary tests mirroring the existing
  `test_as_of_boundary_is_half_open_in_cypher_too`.
- **Null windows:** a `valid_from` of null means valid from the beginning; a
  `valid_until` of null means no known end. This matches `lifecycle.py`.
- **No overwrite semantics.** The API is read-only. Superseded facts remain
  visible under `as_of` and `all`, and are linked to their successors via
  `supersedes` edges when `include=memories`.
- **`CONFLICTING`** memories and edges appear in `current` (believed but
  disputed), carry their status, and are linked by `conflicts_with` when both
  are in the slice. Neither is hidden or preferred.
- **`MERGED`** memories and merged-away entities are excluded from slices by
  default, consistent with retrieval (ADR 0010). They stay directly
  resolvable by id. Canonical entities can report `merged_from_count` with
  `include=merges`.

---

## 14. Security and Isolation

**Identity (unchanged, stated plainly).** Identity remains the ADR 0005
`X-User-Id` placeholder. Phase 8 does not add authentication. The graph API
is as trustworthy as every other endpoint: localhost/development only. It
aggregates more of a user's data per request than any other endpoint:
neighbourhoods, memory content, and resolved message and document text. That
makes it the highest-impact surface if exposed, which is exactly why
authentication must precede any non-local exposure (§24). The existing
startup guard (`APP_ENV=production` + header trust = refuse to start) remains
the enforcement.

**Isolation invariants, each tested:**

1. Every node in every matched path and every relationship carries a
   `$user_id` filter, the same discipline as `NEIGHBOURHOOD_NODES_TEMPLATE`.
2. Every new Cypher constant is registered for the static audit, and the
   §7.4 meta-test makes it impossible for a new module to escape.
3. Every id-accepting parameter — path ids, `exclude_ids`,
   `source_entity_id`/`target_entity_id`, `memory_id` — is tested with
   another user's real ids. The result is 404, or no effect, and never data.
   An `exclude_ids` containing foreign ids must not influence results or
   timing-leak existence (the ids are simply never matched).
4. Merge redirect resolution cannot cross users. This is already enforced by
   `merge` refusing cross-user operations and by `resolve` being
   user-scoped; it is re-tested through the new endpoints.

**Resource exhaustion.** Hard server-side budgets, which a client can lower
but never raise past the setting's cap. Also validated list lengths and
string lengths, and the integer-only interpolated hop bound
(`validate_hops`). Server-side Neo4j query timeouts are considered only if
Stage 0/7 measurement shows a tail-latency need. They are not added blindly.

**No raw Cypher, no injection surface.** Filters are parameters. Predicates
are matched as property values (ADR 0003's generic `:RELATES_TO` design is
what makes this possible without dynamic relationship types). Enum filters
are validated against Python enums.

**Untrusted content for the future UI.** Memory content and document text can
contain markup or script, since documents are an untrusted cross-trust source
(ADR 0007). The API returns plain strings. The UI phase must render them as
text, never as HTML. This is recorded here as a contract requirement for the
consumer.

**Rate limiting** remains absent (CLAUDE.md §27). It belongs with the
authentication phase. Graph endpoints should be its first target, as the most
expensive reads.

---

## 15. Evaluation Strategy

Evaluation is defined before implementation. It is **fully deterministic,
with no LLM involved**, and therefore unaffected by free-tier quota.

**Baseline (Stage 0, measured before any production change):**

- The existing `GET /entities/{id}/graph` on synthetic graphs at 100 / 1k /
  10k nodes with hub entities (extending `eval/graph_scale_benchmark.py`,
  which today never exercises this endpoint), at hops 1–4. Record latency,
  node count, edge count, and serialized payload bytes.
- The request count and total payload to answer three canonical explorer
  questions with today's endpoints:
  - "What is connected to X?"
  - "Why does the system believe X uses Y?" (edge → memories → provenance)
  - "What did X use in March?"
- Characterization tests capturing current behaviour: merged-away entity in
  search, no `as_of` on graph, unbounded node count.

**Target behaviour, measured after:**

- Every new endpoint's response is within its configured budget at every
  benchmark scale. This is a hard assertion, not an average.
- Truncation flags are exact: the response is truncated iff more in-scope
  results existed.
- The three canonical questions are answerable in fewer requests. The exact
  counts are measured, not promised here.

**Conformance dataset** (`eval/datasets/graph_api_v1.jsonl`, new during
implementation). This is a deterministic fixture graph plus expected slices,
seeded through `MemoryService`, the review services, and document ingestion,
exactly as existing harnesses seed:

| Category | What it pins |
|---|---|
| `temporal_current` | EasyOCR→PaddleOCR supersession: `current` shows only PaddleOCR |
| `temporal_as_of` | `as_of` March shows EasyOCR; boundary instant exactly at `valid_until` excluded (half-open) |
| `temporal_all` | Both edges, correct `temporal_state` labels |
| `merged_entity` | Merged-away entity absent from overview and search; neighbourhood by old id returns canonical with `resolved_from` |
| `conflicting` | Both conflicting memories visible, linked by `conflicts_with`, neither preferred |
| `duplicate` | `MERGED` duplicate excluded by default; canonical shows all corroborating sources in evidence |
| `multi_assertion` | Two memories asserting one triple → one slice edge, `assertion_count=2`, both `memory_ids` |
| `bare_statement` | Relationship-less memory visible only via `include=memories` |
| `document_provenance` | Evidence resolves to document, chunk, page, section |
| `hub_truncation` | Hub neighbourhood: `truncated_nodes=true`, exactly budget-many nodes, deterministic set |
| `incremental_expansion` | `exclude_ids` returns only new nodes, plus connecting edges |
| `cross_user` | Every endpoint and every id parameter with another user's ids: no data |

**Metric:** exact match of expected node-id and edge-id sets per task
(precision and recall must both be 1.0), plus field assertions (temporal
states, flags, `resolved_from`). Results are persisted through the Phase 7
`EvaluationRunTracker` (`harness="graph_api"`, `mode="deterministic"`) and
compared run-over-run via `eval/report.py`. This is also the first
deterministic consumer of the Phase 7 persistence layer.

**Failure categories tracked:**

- budget violation,
- truncation-flag error,
- temporal misclassification (especially boundary instants),
- identity error (merged-away leak or lost redirect),
- provenance mismatch,
- isolation violation (any cross-user datum is a critical failure, never
  averaged),
- nondeterministic ordering.

**Ablations** (cheap, informative): current-edge-only vs `all` payload size;
`include=memories` payload growth per `max_memories_per_entity`; degree vs
`updated_at` overview ranking cost.

**No results are asserted in this document.**

---

## 16. Performance Strategy

Measure first; decide second.

**What to measure (Stage 0 and Stage 7):**

- p50/p95 latency per endpoint;
- node and edge counts;
- serialized payload bytes;
- truncation frequency;
- Neo4j `PROFILE` db-hits for each query shape at 100 / 1k / 10k nodes,
  hub fraction as in the existing benchmark, hops 1/2/3.

**Query-shape decision (evidence-gated).** The existing templates enumerate
variable-length paths (`*1..n`) and aggregate `min(length(path))`. Through
high-degree hubs, path count can grow combinatorially before aggregation.
Candidate shapes to compare by measurement:

- **(a)** the existing variable-length pattern plus `LIMIT`;
- **(b)** per-hop `DISTINCT` expansion with a per-hop limit.

Newer Cypher path features available on Neo4j `2026.08.1` may be considered
only if measured to help. No APOC or GDS plugin is introduced.

**Degree-ranked overview cost** at 10k nodes is measured before being adopted
(fallback: `updated_at DESC`, §9.1).

**Targets.** Latency targets are set from the measured Stage 0 baseline, with
rationale recorded. They are not invented in advance. The only fixed targets
are structural: bounded payloads and exact truncation at every scale.

Results are reported as one local machine, one Community instance, synthetic
data — never as a production capacity claim (the same caveat as Phase 4).

---

## 17. Zero-Cost Strategy

- **Infrastructure:** local Neo4j Community `2026.08.1` and local PostgreSQL,
  both already in use. No managed database, no cloud service.
- **Dependencies:** none new. FastAPI, Pydantic, the Neo4j driver, and
  SQLAlchemy already cover everything. No APOC or GDS plugins, no Redis or
  caching layer, no graph-visualization SaaS, no hosted API gateway.
- **Evaluation:** fully deterministic. No LLM calls and no embedding calls
  are needed for the conformance harness, which means no API key, no quota,
  and no internet requirement.
- **Benchmarking:** extends the existing local `graph_scale_benchmark.py`.
- **Real-LLM access:** not used by Phase 8 at all. The Gemini free tier
  remains optional for the unrelated resumable Phase 7 runs.

Nothing in the design normally requires a paid service, so no "free
alternative" substitution was needed and nothing is weakened to appear free.

---

## 18. Implementation Sequence

Each stage is independently testable and revertable. No stage changes
production behaviour before its measurement or characterization gate.

**Stage 0 — Measure and characterize (no production change)**

- *Objective:* establish the baseline in §15/§16; prove current gaps with
  tests rather than asserting them.
- *Files:* `eval/graph_scale_benchmark.py` (add the interactive
  `neighbourhood()` and payload sizing); new characterization tests for
  merged-away search hits, the unbounded node count, and the missing graph
  `as_of`; a round-trip counting script for the three canonical questions.
- *Verification:* recorded numbers; characterization tests pass (documenting
  current behaviour).
- *Decision output:* D1 resolved with evidence; provisional budgets
  confirmed or adjusted; query shape chosen.
- *Risk:* none to production.

**Stage 1 — Projection core: schema, service, repository, audit gap**

- *Objective:* `GraphSlice` schemas; `GraphExplorationService` (budget
  clamping, temporal-scope resolution, redirect resolution); a
  `graph_repository.py` bounded neighbourhood (entity-only); the §7.4
  meta-test plus audit registration.
- *Tests:* unit (schema validation, clamping, the `temporal_state` function
  with half-open boundaries, Cypher audit); integration (neighbourhood
  correctness, cross-user on every parameter).
- *Verification:* conformance categories `temporal_*`, `hub_truncation`,
  `incremental_expansion`, `cross_user` pass for entity-only slices.
- *Rollback:* router not yet mounted, or unmount it; purely additive.

**Stage 2 — `GET /graph/entities/{id}/neighborhood` and `GET /graph/overview`**

- *Objective:* mount the router; overview ranking per the Stage 0 decision.
- *Tests:* API-level tests; performance assertions at 10k.
- *Rollback:* unmount the router.

**Stage 3 — `GET /graph/edges/evidence`**

- *Objective:* one-call relationship → memories → provenance, reusing
  `MemoryService` and `ProvenanceService`.
- *Tests:* `multi_assertion`, `duplicate`, `document_provenance`; cross-user;
  a degraded (unresolvable) source.
- *Rollback:* additive.

**Stage 4 — `include=memories` / `include=merges`**

- *Objective:* memory nodes via `MENTIONS` (bounded per entity);
  `supersedes`/`conflicts_with`/`duplicate_of` among in-slice memories;
  `merged_from_count`.
- *Tests:* `bare_statement`, `conflicting`, `merged_entity`; payload-growth
  ablation.
- *Rollback:* additive parameter.

**Stage 5 — `GET /graph/memories/{id}/timeline` plus history-walk extraction**

- *Objective:* extract the walk from `tools.get_memory_history` into a shared
  function; add the endpoint.
- *Tests:* equality test proving the agent tool's output is unchanged for a
  fixed fixture; all existing agent-tool tests pass; timeline ordering and
  bounds.
- *Risk:* the only stage touching agent-adjacent code. Mitigated by the
  equality test; trivially revertable.

**Stage 6 — Decision-gated fixes to existing endpoints (D2, and D1 if justified)**

- *Objective:* `include_merged=false` on `/entities/search`; the optional
  `/entities/{id}/graph` cap.
- *Tests:* Stage 0 characterization tests are updated to the new, approved
  behaviour. `test_seed_demo.py`, `test_graph_retrieval.py`, and
  `test_api.py` graph tests pass or are updated only where the approved
  behaviour change requires it, with each change justified.
- *Rollback:* single-parameter defaults.

**Stage 7 — Conformance harness and before/after**

- *Objective:* `eval/graph_api_runner.py` plus `graph_api_v1.jsonl`,
  persisted via `EvaluationRunTracker`; re-run the benchmark on the new
  endpoints; before/after request counts and payloads vs Stage 0.
- *Verification:* 100% conformance; budgets hold at every scale; latency
  targets set from the baseline and met, or honestly reported if not.

**Stage 8 — Documentation and ADRs**

- *Objective:* `docs/API.md` (new endpoints; correct the §2.4 inaccuracy);
  `docs/ARCHITECTURE.md` §9/§23 graph-exploration notes; ADRs per §7.6;
  README roadmap and ADR 0005 status line per D0; a Phase 8 implementation
  status section in this file.

**Stage 9 — Full regression, zero-cost verification, checkpoint**

- *Objective:* full suite, `ruff`, deterministic evaluations (retrieval,
  document, graph API), cleanup verification of synthetic eval data, one
  checkpoint commit (on approval).

---

## 19. Testing Strategy

**Unit (no database):**

- `GraphSlice`/parameter schema validation: budgets, clamps, enum filters,
  `exclude_ids` length and format, predicate limits.
- The `temporal_state` classifier: every boundary (`== valid_from`,
  `== valid_until`, null windows), mirroring `lifecycle.py` tests.
- Relationship-aggregation grouping logic.
- Cypher static audit: new constants user-filtered on every node and edge;
  only the hop bound interpolated; the §7.4 meta-test.

**Integration (real Neo4j + PostgreSQL, skips cleanly without services):**

- Every endpoint against the §15 fixture categories.
- Cross-user tests for every endpoint and every id-accepting parameter.
- Merged-entity redirect through every entry point.
- Document provenance through evidence.
- Truncation exactness on a hub fixture.
- Deterministic ordering (the same request twice yields an identical
  response).

**Regression:**

- The full existing suite (769 at baseline).
- Agent-tool integration tests plus the new equality test (Stage 5).
- Retrieval: `eval/retrieval_runner.py` and the hybrid-retrieval tests yield
  identical results.
- Existing `/entities/*` tests; `test_seed_demo.py`.
- The existing Cypher and SQL safety audits.

**Evaluation:** the §15 conformance harness (deterministic, persisted), plus
the benchmark (§16).

**Hygiene:** every new harness cleans up its synthetic users in both
PostgreSQL and Neo4j, using the same FK-safe order as
`tests/integration/conftest.py`. This avoids repeating the Phase 7-found
`agent_runner.py` leak pattern.

---

## 20. Completion Criteria

Phase 8 is complete when all of the following are true:

1. `/graph/overview`, `/graph/entities/{id}/neighborhood`,
   `/graph/edges/evidence`, and `/graph/memories/{id}/timeline` are
   implemented, documented, and tested.
2. Every new endpoint's response is within its configured node and edge
   budgets at 100 / 1k / 10k benchmark scales, and truncation flags are
   exact. Both are verified by assertion.
3. The conformance harness passes 100%: exact node and edge set match on
   every task, zero isolation failures. It is persisted as a Phase 7
   `evaluation_runs` row.
4. Temporal slicing (`current` / `as_of` / `all`) is correct at boundary
   instants, and matches `get_entity_state`'s answer for the same entity and
   instant.
5. Merged-away entities never appear in overview, neighbourhood, or search
   (post-D2) by default, and redirects report `resolved_from`.
6. Every relationship in a slice traces to its memories and sources in one
   request.
7. Before/after measurements (request count, payload, latency) vs the
   Stage 0 baseline are recorded with real numbers.
8. `AgentService` tool outputs and retrieval evaluation results are unchanged
   (proven).
9. The Cypher audit covers every repository module containing Cypher, by
   meta-test.
10. All pre-existing tests pass; `ruff check app tests eval` is clean.
11. Zero-cost verified: no new dependency, no paid service, no LLM required.
12. Documentation and ADRs are updated per Stage 8, including the API.md
    correction and the roadmap/ADR 0005 status update per D0.

---

## 21. Risks

| Risk | Mitigation |
|---|---|
| Path explosion through hubs makes bounded queries slow even with `LIMIT` (enumeration before limiting) | Stage 0 `PROFILE` measurement; compare query shapes (§16); hard hop cap of 3 |
| Truncated slices mislead (a partial picture presented as whole) | Exact `truncated_*` flags; deterministic priority ordering (in-scope, fewer hops, then name, then id); the contract requires the UI to surface truncation |
| Temporal semantics drift between the new Cypher and `lifecycle.py` / `get_entity_state` | Reuse the memory-search `as_of` predicate form; boundary tests; completion criterion 4 cross-checks against `get_entity_state` |
| Behaviour change to existing endpoints breaks consumers | D1/D2 are decision-gated, characterization-tested first, and additive where possible; the only consumers are tests and `seed_demo.py` (verified) |
| New Cypher escapes the isolation audit | §7.4 meta-test (closes the gap generally) |
| The largest-aggregation endpoint is exposed without auth | Unchanged startup guard; explicit dev-only statement; auth precedes any exposure (§24) |
| Duplicating agent history logic | Extraction plus equality test (§7.3) |
| Payload growth from memory content | Per-entity memory budget; content truncation in slices (full text via `/memories/{id}`) |
| Scope creep into a graph query language or UI concerns (layout, styling) | §22 non-goals; the fixed parameter set in §9 |
| Degree-ranked overview too expensive at scale | Measured; documented fallback ordering |
| Roadmap deviation (D0) creates confusion with ADR 0005's "replaced in Phase 8" | Stage 8 updates the ADR 0005 status line and the README roadmap once approved |

---

## 22. Non-Goals

Phase 8 will not implement:

- any frontend, graph rendering, layout computation, or UI state;
- authentication, sessions, user management, or rate limiting;
- deployment, CI deploy workflows, or hosting;
- graph writes or edits through the exploration API (mutation stays in the
  existing memory, review, and invalidate endpoints);
- a graph query language, GraphQL, free-form filter expressions, or any raw
  Cypher surface;
- new Neo4j labels, relationship types, or constraints, or any PostgreSQL
  schema change;
- changes to retrieval ranking, the hybrid retrieval architecture, or the
  agent, its prompt, or its tools' behaviour;
- memory consolidation, decay, importance scoring, or lifecycle automation;
- LLM-assisted entity-resolution adjudication;
- a local/open-source LLM provider, or any real-LLM evaluation work;
- streaming, websockets, caching layers, Redis, queues;
- graph analytics (centrality, communities) or APOC/GDS plugins;
- bulk export.

---

## 23. Deferred Work

| Item | Why deferred | Recommended placement |
|---|---|---|
| Authentication + productionization (the original Phase 8) | Required before exposure; orthogonal to Phase 8 via the ADR 0005 seam | **Phase 9** |
| Rate limiting (graph endpoints first) | Belongs with identity | Phase 9 |
| Observability (structured logs/traces) | Pairs with productionization | Phase 9 |
| Memory Explorer UI (graph, entity pages, provenance, time slider) | Consumes the Phase 8 contract; ideally after auth | Phase 10 (see §24) |
| Review-workflow UI, document management UI | Backends exist; UI phase | With the Explorer UI |
| Local LLM provider (e.g. Ollama) for zero-cost real-LLM evidence | Evidence would be about a different model than configured (ADR 0016); structured-output risk | Standalone, bounded task; any time |
| Resume Phase 7 real-LLM runs | Operational, not a capability: `EvaluationRunTracker` already resumes | When Gemini quota recovers |
| `eval/agent_runner.py` synthetic-user cleanup gap (Phase 7 finding) | Unrelated to the graph API | Small standalone hygiene fix |
| Memory consolidation / decay / importance | No usage signal; canonical writes on a schedule | After the Explorer exists (a visibility-driven signal) |
| LLM-assisted entity adjudication | Identity-influence risk; no measured bottleneck | Revisit with review-volume data |
| Graph analytics (GDS community detection, centrality) | Would add a plugin dependency; no evidenced need | Not scheduled |

---

## 24. Expected Future Phase Dependencies

```
Phase 8  Memory Graph Exploration API  (this plan)
   |   bounded, temporal, provenance-linked read contract; deterministic conformance
   v
Phase 9  Authentication + productionization
   |   identity replaces the ADR 0005 header in get_current_user_id -- every
   |   /graph/* endpoint inherits it with no endpoint change; rate limiting
   |   (graph endpoints first); observability; production config
   v
Phase 10 Memory Explorer UI + deployment
       consumes /graph/* exactly as specified; renders content as text (§14);
       surfaces truncation; time slider drives temporal_scope/as_of;
       review/document UIs reuse the Phase 4/6 endpoints
```

**Why auth before the UI.** A browser UI is only useful beyond a single
developer's machine once identity is real. A UI built before auth would need
its request layer, error handling, and navigation reworked for login and
session flows.

**Viable alternative.** Build a *local, single-user* Explorer immediately
after Phase 8 against the development placeholder, and move auth after it.
That shortens the path to a visible product at the cost of that retrofit.
This choice can be made when Phase 9 is planned, informed by Phase 8's
finished contract. It does not need to be made now.

**What Phase 8 guarantees to its successors:**

- a documented, tested read contract whose semantics are server-authoritative;
- no dependency on the identity mechanism beyond `CurrentUser`;
- budgets and truncation designed for incremental client rendering;
- a deterministic conformance harness that later phases re-run as a
  regression gate whenever the canonical graph model or identity layer
  changes.

---

## 25. Implementation Status (Phase 8 complete)

Every number below comes from a real run on one local machine against local
Neo4j Community `2026.08.1` and local PostgreSQL, using synthetic data. They
are relative comparison points, not production capacity claims.

### 25.1 Decisions as applied

| Decision | Applied as |
|---|---|
| D0 | Phase 8 = memory graph exploration API. Authentication moved to Phase 9 (README roadmap, ADR 0005 status line, auth-related code comments and the `ALLOW_USER_ID_HEADER=false` error message). The roadmap follows §24: Phase 10 = Memory Explorer UI + deployment. |
| D1 | **Changed, because Stage 0 measured a concrete need** (§25.2). This was the smallest change available: `GRAPH_MAX_NODES=1000` / `GRAPH_MAX_EDGES=5000` caps, fetched as cap + 1, plus an additive `Neighborhood.truncated`. Shape, ordering semantics (plus an `id` tiebreak) and temporal behaviour are otherwise unchanged. |
| D2 | `/entities/search?include_merged=false` by default. A merged-away name is still found through the canonical entity's alias, merged ids still resolve, and isolation holds. Agent behaviour is unaffected: only the route calls entity search. |
| D3 | `current` is the default for overview, neighborhood **and evidence**. §9.3 had proposed `all` for evidence; the explicitly approved D3 ("new graph endpoints default to CURRENT") takes precedence, so historical evidence needs `temporal_scope=all`. The timeline is always `all`, because calling it is itself an explicit request for history. |

### 25.2 Stage 0: the existing `GET /entities/{id}/graph` (before any change)

`eval/graph_endpoint_benchmark.py` reuses `_seed_graph` (30% of edges
superseded) and calls `RetrievalService.get_neighbourhood` exactly as the
route builds it: `GRAPH_MAX_HOPS=4`, 3 trials, 60 s client timeout.

| Scale | Root (degree) | hops | median | nodes | edges | payload |
|---|---|---|---|---|---|---|
| 100 | hub (22) | 4 | 89 ms | 99 | 233 | 121 KB |
| 1k | hub (205) | 1 | 75 ms | 204 | 377 | 212 KB |
| 1k | hub (205) | 2 | 492 ms | 791 | 3,073 | 1.45 MB |
| 1k | any | 3–4 | 0.64–1.13 s | ≈ whole graph (998–999) | ≈ 3,970 | 1.86 MB |
| 10k | low (24) | 1 | 134 ms | 24 | 119 | 55 KB |
| 10k | low (24) | **2 (route default)** | **45.3 s** | 9,922 | 218,238 | **89.0 MB** |
| 10k | low (24) | 3 | 58.4 s — **timeout** | 9,999 | 219,782 | 89.6 MB |
| 10k | medium (31) | 2 | **timeout (>60 s)** | — | — | — |
| 10k | high (41) | 2 | 55.4 s — **timeout** (max 62.6 s) | 9,994 | 219,700 | 89.6 MB |
| 10k | hub (2,034) | 1 | 5.6 s | 2,026 | 23,555 | 9.9 MB |
| 10k | hub (2,034) | 2–4 | 41.0–44.5 s (max 72.0 s) | 9,999 | 219,782 | 89.6 MB |

Merged-away root: resolves to the canonical entity, but the response carries
no redirect information. Cross-user: `NotFoundError` (404). Superseded edges
are returned by default (the endpoint has no temporal filter).

**Timeout behaviour.** One request ran to 72 s despite the 60 s
`asyncio.wait_for`. The client-side timeout cannot interrupt the driver's
CPU-bound decode of an ~90 MB result, so a client timeout is not a
substitute for a server-side result bound. The server transaction was
terminated after each timeout (`TERMINATE TRANSACTION`).

**D1 after the cap** (same benchmark, `--label after-D1-cap`):

| Scale | Case | median | nodes | edges | payload | truncated |
|---|---|---|---|---|---|---|
| 100 / 1k | every case | unchanged from above (all below the cap) | same | same | same | false |
| 10k | low/medium h=2..4 | 1.07–1.21 s | 1,000 | 5,000 | 2.30 MB | true |
| 10k | high h=2..4 | 1.39–1.91 s (max 5.6 s) | 1,000 | 5,000 | 2.34 MB | true |
| 10k | hub h=1..4 | 1.25–1.86 s | 1,000 | 5,000 | 2.31 MB | true |

No timeouts remain. The worst payload went from 89.6 MB to 2.34 MB.

### 25.3 The `/graph/*` endpoints (final code)

`eval/graph_api_benchmark.py`: 5 trials, default budgets (150 nodes /
400 edges / hops ≤ 2), `RELATES_TO.memory_id` back-filled so every
asserting-memory check is exercised. **Budget violations across every
measured slice: 0.**

| Scale | Case | p50 | p95 | nodes / edges | truncated |
|---|---|---|---|---|---|
| 100 | every neighborhood case | 3–24 ms | ≤ 212 ms (first call) | ≤ 71 / ≤ 154 | no |
| 100 | overview current / all | 21 / 27 ms | 32 / 31 ms | 100 / 164–233 | no |
| 1k | neighborhood h=1 (low…high) | 4–11 ms | ≤ 13 ms | ≤ 13 / ≤ 19 | no |
| 1k | neighborhood h=2 medium/high | 45–52 ms | ≤ 93 ms | 150 / 260–336 | nodes |
| 1k | neighborhood hub h=1..2 | 38–55 ms | ≤ 80 ms | 150 / 194–204 | nodes |
| 1k | overview current / all | 31 / 41 ms | 35 / 42 ms | 150 / 101–135 | nodes |
| 10k | neighborhood h=1 low/medium/high | 139–380 ms | ≤ 432 ms | ≤ 42 / ≤ 341 | no |
| 10k | neighborhood h=2 low/medium/high | 0.50–1.24 s | ≤ 1.28 s | 150 / 400 | nodes + edges |
| 10k | neighborhood hub h=1..2 | 120–146 ms | ≤ 160 ms | 150 / 249–314 | nodes |
| 10k | neighborhood `include=memories` h=1 | 143–247 ms | ≤ 249 ms | ≤ 150 / ≤ 249 | per case |
| 10k | overview current / all | 116 / 121 ms | 133 / 127 ms | 150 / 85–121 | nodes |
| 10k | evidence on a hub edge | 4.0 ms | 107 ms (first call) | — | — |

Payloads are bounded at every scale. The largest measured `/graph/*` payload
is 230 KB, against 89.6 MB from the pre-Phase-8 endpoint at the same scale.

**Two measured optimisations before these numbers** (a `PROFILE` on a
seeded 10k graph; no index was needed, since every lookup is a
`NodeUniqueIndexSeek` on an existing constraint):

1. *Filter order.* In the edge-collection query, the per-edge
   asserting-memory lookup ran before the cheap `b.id IN $reach_ids` filter,
   on every edge of every hub in the slice. A 20-node slice cost 293,314
   db-hits and took 359 ms warm. The predicate was split, and the lookup now
   runs behind a `WITH DISTINCT` barrier after every cheap filter. The 10k
   hub neighborhood dropped from 0.23–0.31 s to 0.12–0.15 s, and the worst
   neighborhood from 1.68 s to 1.24 s p50.
2. *Overview ranking.* Ranking by in-scope degree measured 3.8 s warm
   (5,522,071 db-hits) at 10k, and 6.4–6.8 s p50 in the benchmark. That
   fails the target set from the measurements: a landing slice should be
   no slower than the slowest neighborhood (≈ 1.2–1.7 s). Per §9.1 the
   approved fallback, `updated_at DESC, name, id`, was adopted: 62 ms warm
   (61,375 db-hits), 116–121 ms p50 end to end. A measured alternative, *not
   adopted without approval*, is ranking by stored `RELATES_TO` degree
   (`getDegree`, O(1) per node). It took 74 ms warm, and its top 150 overlap
   the in-scope-degree top 150 on 118 entities. It counts historical and
   duplicate assertions too.

**Evidence: no N+1** (dedicated fixture):

| Case | p50 | assertions | status | Neo4j reads / request | SQL statements / request |
|---|---|---|---|---|---|
| no asserting memory | 2.7 ms | 1 | `no_asserting_memory` | 3 | 0 |
| one memory, no sources | 2.5 ms | 1 | `no_sources` | 3 | 0 |
| one memory, one source | 3.7 ms | 1 | `available` | 3 | 1 |
| 10 memories × 1 source | 9.2 ms | 10 | `available` | 3 | 1 |
| 1 memory × 10 sources | 4.1 ms | 1 | `available` | 3 | 1 |

Round trips are constant: 2 entity resolutions + 1 evidence read, and at
most 3 batched SQL statements (messages / documents / chunks).
`ProvenanceService.get_provenance` now uses the same batched resolver.

**Timeline** (bounded at 20): chain length 1 / 5 / 20 / 25 took
0.9 / 5.5 / 18.8 / 46.3 ms and returned 1 / 5 / 20 / 20 nodes. The
length-25 chain is flagged `truncated_nodes=true`.

**Canonical explorer questions** (real HTTP requests through the app):

| Question | Old endpoints | `/graph/*` |
|---|---|---|
| "What is connected to X?" | 1 request / 4,866 B (unbounded in general) | 1 / 1,742 B (bounded) |
| "Why does the system believe X uses Y?" (10 memories) | **21 requests** / 34,791 B | **1** / 21,168 B |
| "What did X use in March?" as a drawable graph | 1 / 9,834 B (a list, not a graph slice) | 1 / 1,733 B |

### 25.4 Conformance harness

`eval/graph_api_runner.py` + `eval/datasets/graph_api_v1.jsonl`
(39 tasks, 13 categories). It is persisted through `EvaluationRunTracker`
(`harness=graph_api`, `mode=deterministic`). Every task runs twice and must
be byte-identical; every slice is budget-checked.

| Run | Result | Note |
|---|---|---|
| `558ec861…` | 38/39 | **Scorer bug, not a service bug**: the absent-label check used substring matching, so "OCR" matched "PaddleOCR". The scorer was fixed (exact for entity names; substring only on memory labels). |
| `0f88f9f4…` | 39/39 | before the §25.3 query optimisations |
| `86419648…` | **39/39**, mean precision = mean recall = 1.0, 0 isolation failures | final code |

The harness also runs inside the test suite
(`tests/integration/test_graph_api_conformance.py`, unpersisted).

### 25.5 Regression proofs

* **Agent tool unchanged.** `tests/integration/test_memory_history_walk.py`
  compares `get_memory_history`'s full output, order included, against a
  frozen verbatim copy of the pre-extraction code. The fixture covers
  supersession both ways, a duplicate, a conflict (still not followed by the
  agent walk) and a chain longer than the bound. It passed against the
  unmodified code first, then after the extraction. Every existing
  agent-tool test passes. One subtle difference: `now_fn()` is now sampled
  after the walk rather than between the first read and the walk. With
  frozen clocks the output is identical; with a real clock the difference
  is the walk's few milliseconds.
* **Retrieval unchanged.** `eval/retrieval_runner.py` output is
  byte-identical between a pristine `HEAD` worktree and the Phase 8 tree:
  12 scenarios, graph-only / vector-only / hybrid.
* **Documents unchanged.** The `eval/document_runner.py` deterministic
  section passes 10/10 with 0 wrong merges.
* **Full suite.** **942 passed** (baseline 769, +173), 0 failed.
  `ruff check app tests eval` is clean. The 3 warnings are third-party
  deprecations (starlette / anyio / google-genai).

### 25.6 Findings

1. **Neo4j DateTime ordering is zone-sensitive at equal instants.**
   `datetime('…[UTC]') <= datetime('…Z')` is **false**. Merge-copied
   `RELATES_TO` windows are re-written from driver values and carry a named
   `UTC` zone, while Python parameters carry an offset. A naive `r.valid_from
   <= $at` therefore silently excluded a merge-copied edge at exactly its
   `valid_from`. The failing boundary test caught it, where `get_entity_state`
   (a Python comparison) disagreed. Every Phase 8 window comparison now
   normalises both sides to UTC, and the static audit checks this. The
   existing memory-search `as_of` fragment uses the unnormalised form; memory
   windows are always written from Python, so no case is known to hit it
   today. It is noted, not changed (out of scope).
2. **Naive `as_of` would silently empty a slice.** A LocalDateTime never
   compares with a stored DateTime, so `/graph/*` now rejects it with 422.
   Other endpoints accept naive datetimes; not changed.
3. **The client timeout cannot bound a huge result** (§25.2). Server-side
   result bounds are the only real protection.
4. **Pre-existing test residue.** The test DB holds 63 `itest-*` users from
   earlier sessions. Running every integration file individually today leaks
   none, and the Phase 8 tests leak none (count checked before and after).
   They were left in place.
5. `RetrievalService.get_relationships` / `get_entity_state` pass the
   *requested* id, not the resolved canonical id, to the relationship query.
   For a merged-away id they read the merged node's own (still-present)
   edges. This is pre-existing, observed and not changed. The Phase 8
   endpoints always use the resolved id.

### 25.7 Known limitations

* The overview ranks by **recency**, not connectivity (§25.3). The stored-degree
  alternative awaits a decision.
* Truncation flags report **budget** cuts only. Nodes beyond the requested
  hop horizon are out of scope by definition and are not flagged.
* An aggregated `relates_to` edge reports `valid_from` = the earliest and
  `valid_until` = the latest window among its assertions (null if any is
  open-ended). Per-assertion windows are in the evidence endpoint.
* Evidence is keyed by the **directed** triple. The reverse direction is a
  separate request.
* The timeline makes one `MemoryService.get` per chain member (≤ 20). That
  is bounded, but not a single query.
* `truncated_memories` is conservative when the node budget, rather than the
  per-entity cap, cuts memories.
* The automated benchmark measures wall-clock time only. The db-hit figures
  above come from a one-off `PROFILE` session, not a committed harness.
* There is still no authentication or rate limiting (Phase 9). `/graph/*`
  aggregates the most user data per request and must be their first target.

### 25.8 Zero-cost verification

No new dependency (`pyproject.toml` / `uv.lock` unchanged), no migration, no
new Neo4j label, relationship type, constraint or index, and no PostgreSQL
schema change. No LLM or embedding call anywhere in Phase 8's code, tests,
harness or benchmarks. The new modules import only the standard library and
existing dependencies. `frontend/` is untouched. All runs used local Neo4j
Community and local PostgreSQL. Synthetic benchmark and eval users are
cleaned up in both stores; 0 `bench-*` / `eval-graph*` users or nodes
remain.
