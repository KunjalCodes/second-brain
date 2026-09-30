# Phase 15 — The Memory Graph As Implemented (M0 audit, 2026-09-29)

This describes the graph at `2ecb83f` (Phase 14), before any Phase 15 change.

**Evidence labels:**

| Label | Meaning |
|---|---|
| [I] | Inspected in code (`path:line`, relative to `backend/`) |
| [V] | Verified by a test that ran |
| [L] | Limitation |

Where Phase 14 already documented a part in depth, this document points there
(`docs/PHASE14_MEMORY_MODEL.md`) rather than repeating it.

## 1. The nine representations, kept distinct

| # | Concept | Where it lives | Canonical? |
|---|---|---|---|
| 1 | **Canonical memory** | Neo4j `:Memory` (`content`, `memory_type`, `status`, `confidence`, `valid_from`, `valid_until`, `created_at`, `updated_at`, `invalidation_reason`). Content is immutable | **Yes, the unit of truth** (ADR 0001) |
| 2 | **Entity** | Neo4j `:Entity` (`name`, `normalized_name`, `entity_type`, `aliases[]`), unique per `(user_id, normalized_name)` | Yes, but identity is name-based |
| 3 | **Relationship** | Neo4j `:RELATES_TO` Entity→Entity, one edge per *assertion*, with `predicate`, `status`, `confidence`, `valid_from`, `valid_until` and **`memory_id`** (the asserting memory) | Yes, but derived from its memory: supersede and invalidate close edges by `memory_id` |
| 4 | **Source** | Neo4j `:Source` (`source_type`, `reference`, `location`), deduplicated per user on `(type, reference‖location)` | Yes (a pointer) |
| 5 | **Provenance** | `(Memory)-[:DERIVED_FROM]->(Source)`, resolved by `ProvenanceService` into the PostgreSQL message, document or chunk (`resolved` flag since Phase 14). The edge side is `RELATES_TO.memory_id` | Derived at read time |
| 6 | **Temporal state** | Stored windows `[valid_from, valid_until)` plus `status`. `current` / `historical` / `not_yet_valid` are **computed** at a reference instant (`schemas/graph.py:77-91`, `_EDGE_TEMPORAL_STATE` `graph_repository.py:95-99`) | Computed, never stored |
| 7 | **Derived graph views** | Phase 8 `GraphSlice` (overview, neighbourhood, timeline). `relates_to` slice edges are **aggregated** per `(source, predicate, target, status, state)`, with `memory_ids` (capped at 20) and `assertion_count` (`graph_repository.py:133-156`) | Projection only |
| 8 | **Retrieval-only relationships** | Hybrid retrieval's graph channel: hop distance and path (`GraphHit`, `graph_path`) from the query's entity mentions (`hybrid_retrieval_service.py` `_graph_candidates`) | Never stored |
| 9 | **UI-only representations** | Cytoscape elements (`frontend/lib/graph/model.ts` `toElements`): truncated labels, `merged` flag, weights. The list view. The 600-element canvas cap | Client state only |

**No representation duplicates canonical memory.** PostgreSQL holds audit,
jobs, embeddings and review rows keyed by memory id, never memory content as
truth (ADR 0006) [I].

## 2. Neo4j schema [I]

**Nodes:** `Memory`, `Entity` and `Source` only.

**Relationships:**

| Relationship | Meaning |
|---|---|
| `MENTIONS` | Memory→Entity |
| `DERIVED_FROM` | Memory→Source |
| `RELATES_TO` | Entity→Entity |
| `SUPERSEDES` | new→old Memory |
| `CONFLICTS_WITH` | Memory–Memory |
| `DUPLICATE_OF` | dup→canonical Memory |
| `MERGED_INTO` | merged-away→canonical Entity |

**Constraints** (`app/graph/schema.py`): unique `Memory.id`, `Entity.id`,
`Source.id`, and `(Entity.user_id, normalized_name)`.

**Indexes:**
- Memory: `(user_id, status)`, `(user_id, created_at)`, `(user_id, memory_type)`;
- Entity: `(user_id, entity_type)`;
- Source: `(user_id)`;
- RELATES_TO: `(user_id, status)`, `(predicate)`, `(memory_id)`.

**Isolation:** `user_id` is on every node and on every RELATES_TO,
CONFLICTS_WITH, DUPLICATE_OF and MERGED_INTO edge. Every Cypher constant is
audited for a `$user_id` filter (`tests/unit/test_cypher_safety.py`,
ADR 0019).

## 3. Semantics currently expressible [I]

- **Entity types:** person, project, company, technology, concept, document,
  decision, event, task, other.
- **Predicates:** free text matching `^[a-z][a-z0-9_]{0,62}[a-z0-9]$`.
- **Cardinality:** since Phase 14, 31 predicates are declared multi-valued
  (`decisions.MULTI_VALUED_PREDICATES`). Every other predicate is treated as
  single-valued for conflict detection.
- **Causal relationships:** there is **no notion of causal vs associative
  relationships**. `led_to`, `caused` and `fixed_by` are just strings. Nothing
  creates a causal edge by inference, since only extraction and the API write
  edges [I].

**Supported today with no model change:**
- `Project —uses→ Technology`, `Person —works_on→ Project`
- `Decision —led_to→ Event`, `Task —depends_on→ Task`
- supersession between memories

## 4. Temporal model [I][V]

- **Windows:** half-open `[valid_from, valid_until)`
  (`app/models/lifecycle.py:80-93`).
- **Statuses:** active, historical, superseded, invalid, conflicting,
  archived, merged. Believed = {active, conflicting}. Excluded from default
  retrieval = {invalid, archived, merged}.
- **Graph API scopes** (`schemas/graph.py:28-39`, `scope_params`
  `graph_repository.py:246-254`):
  - **`current`:** believed status and the window contains now;
  - **`as_of`:** any non-excluded status whose window contains the instant;
  - **`all`:** any non-excluded status, any window.
  - **There is no `historical` scope** (only no-longer-true facts) [L].
  - Scope is applied in Cypher on every hop, before the LIMIT.
- **Entity state:** `RetrievalService.get_entity_state` splits current and
  historical at an instant, but **loads every relationship of the entity with
  no bound** (`retrieval_service.py:220-258`) [L].
- **Since Phase 14:** valid time is when a fact was stated, and a supersede
  closes the old fact at the new fact's valid time.

## 5. Lifecycle into the graph

See `PHASE14_MEMORY_MODEL.md` §0–9 and the Phase 14 changes:
- worthiness;
- stated-time validity;
- cardinality;
- merged-entity write redirect.

Graph effects per decision:

| Decision | Graph effect |
|---|---|
| NEW | Memory, entities (MERGE), RELATES_TO |
| SUPERSEDING | The same, plus `SUPERSEDES`; the old memory and its edges are closed |
| CONFLICTING | Memory with status `conflicting`, plus `CONFLICTS_WITH` |
| DUPLICATE / UPDATE | An extra Source, or a confidence change, on the existing memory |

Invalidation closes the memory and its edges and records a reason.

## 6. Merged entities [I]

- **Merge:** `MERGED_INTO` redirect (depth ≤ 5, cycle-refused). Aliases are
  unioned, and `RELATES_TO` edges are *copied* with a `merged_via` tag.
- **Reads resolve redirects** (`RESOLVE_ENTITY`); slices never show
  merged-away nodes.
- **Writes resolve too**, since Phase 14.
- **Reversible** (`reverse_merge`).
- **Retrieval's mention detection ignores aliases.** A query naming an
  alias ("Open AI") resolves only if the merged-away entity still exists with
  that normalized name [I][L].

## 7. Graph exploration API (Phase 8) [I]

**Routes:**
- `GET /graph/overview`
- `GET /graph/entities/{id}/neighborhood` (BFS in Python, one bounded query
  per hop)
- `GET /graph/edges/evidence`
- `GET /graph/memories/{id}/timeline`

**Budgets:** `max_nodes` 150 (≤ 1000), `max_edges` 400 (≤ 3000), hops 2 (≤ 3),
5 memories per entity. They are clamped (a client can only lower them), with
exact truncation flags (`limit+1`).

**Older endpoints:**
- `GET /entities/{id}/graph|relationships|history|memories` (RetrievalService).
- **`relationships` and `history` are unbounded per entity** [L].

**Missing (Phase 15 inputs):**
- path finding between two entities;
- an entity-context view;
- a query-driven context subgraph;
- relationship "why" explanations beyond raw evidence;
- an evolution view with event kinds.

## 8. Retrieval and agent [I]

- **Hybrid retrieval** (vector + term keyword + graph), then merge, temporal
  filter and linear ranking. `RetrievalExplanation` carries per-channel
  signals, `graph_distance` and `graph_path`.
- **The agent's `traverse_graph` tool** uses `get_neighbourhood_with_paths`,
  capped by `retrieval_graph_max_nodes` (200).
- **No evidence packing:** retrieval returns ranked memories only; there is
  no evidence-subgraph packing.

## 9. Frontend graph explorer [I]

See the Phase 12 audit (`docs/PHASE12_PLAN.md` §2.7).

**What exists:**
- Cytoscape canvas with cose layout;
- overview and neighbourhood, expansion, re-root;
- list view;
- node and edge detail, edge evidence;
- 600-element cap, truncation banners;
- time lens (current / as of);
- history toggle (`all`).

**What is missing:**
- entity-type and predicate filters in the UI (the server already supports
  them);
- path finding and path highlighting;
- a historical-only mode;
- collapsing an expansion;
- an evolution view;
- an entity context panel.

## 10. Authentication and isolation [I][V]

- **Owner id:** every route takes its owner id from `CurrentUser`.
- **Unknown or foreign ids** return 404 (entity, memory, evidence).
- **Audits:** Cypher safety, SQL safety, auth audit and the route contract
  inventory.
- **Evidence:** graph conformance includes 8 cross-user tasks (39/39).

## 11. Phase 16 readiness of the current model [I]

The model **can already represent** coding-project history with no schema
change, using the existing entity types and free predicates:

| Coding concept | Existing entity type | Phase 16 would want |
|---|---|---|
| Project | `project` | – |
| Decision | `decision` | – |
| Bug, Error, Fix, Change, AgentSession, Commit | `event` / `task` / `other` | Dedicated types |
| Feature | `task` / `concept` | `feature` |
| File / Class / Function / Directory / Repository / Dependency / Branch | `other` / `technology` | Dedicated types plus exact identity (Phase 12 plan §5) |

**Predicates** (`led_to`, `fixed_by`, `made`, `modified`, `worked_on`,
`changed`, `contains`, `depends_on`) are all valid today.

**What is missing for Phase 16:**
- dedicated entity types;
- a project scope;
- structural (non-LLM) ingestion;
- a notion of "session" ordering and "unfinished";
- the ability to tell causal from associative relationships.

This is Phase 16 (designed in `docs/PHASE12_PLAN.md`).
