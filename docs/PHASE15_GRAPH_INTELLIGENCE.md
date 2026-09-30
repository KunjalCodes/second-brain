# Phase 15 — Graph Intelligence (as built)

**Status:** implemented and verified locally. The graph it reads is described
in `docs/PHASE15_GRAPH_MODEL.md`; results are in `docs/PHASE15_REPORT.md`; the
endpoint reference is `docs/API.md` §4d.

**Evidence labels:**

| Label | Meaning |
|---|---|
| [V] | Verified by a test or check that ran |
| [M] | Measured |
| [I] | Inspected in code |
| [A] | Assumption |
| [L] | Limitation |

## 1. Principles

- **One graph.** No new node labels, no new relationship types, no second
  temporal model, and **no schema or migration change**. Everything is a
  bounded, read-only projection over `Memory` / `Entity` / `Source` /
  `RELATES_TO` [I].
- **Nothing inferred is stored.** A multi-step connection, a relevance score
  or a "why" sentence exists only in a response, labelled as derived. The
  graph never gains an edge that nobody stated.
- **Causality is only what was stated.** A relationship is "causal" if and
  only if the memory's own predicate says so (`led_to`, `fixed_by`, ...).
  Sequence, timing and co-occurrence never create or imply causality [V:
  `test_relation_kinds_come_from_the_stated_predicate_only`, eval J1/J2/Q1].
- **Bounded everywhere.** Server-side caps a client may lower but never
  raise; exact truncation flags; a hard maximum path depth of 4 (default 3).
- **User-scoped everywhere.** Every Cypher constant filters on `$user_id`
  (audited), and a foreign id is a 404.
- **Deterministic.** Equal scores tie-break by name then id; two runs of the
  evaluation are identical apart from latency [V].

## 2. Temporal (M2)

- `TemporalScope` gains **`historical`**: facts that are *no longer true* at
  the reference instant (`as_of`, else now): a closed window
  (`valid_until <= at`) **or** a non-believed status (superseded,
  historical). Excluded statuses (`invalid`, `archived`, `merged`) stay
  excluded [I].
- It is one extra boolean predicate (`$only_past`) inside the existing shared
  scope fragments, so it applies **on every hop, before any LIMIT**, exactly
  like the other scopes. The other three scopes are unchanged; measured
  response sizes of the Phase 8 endpoints are byte-identical and latency is
  within ±3% [M].
- The half-open window `[valid_from, valid_until)` is the only temporal model.
  Example: after "Kunjal moved to Globex", `current` returns Globex and not
  Acme; `historical` returns Acme; `as_of` a date in the Acme period returns
  Acme [V: eval C1, D1, O1].
- **Entity state history:** `GET /entities/{id}/context` splits current and
  historical relationships and lists a timeline of when facts began and ended
  [V].
- **Memory evolution and supersession chains:** `GET /memories/{id}/evolution`
  (§7).
- **Merged entity history:** merge redirects are followed for every entity
  parameter, the response reports `resolved_from`, and `merged_from` lists the
  names merged into the canonical entity [V: eval F1, F2].
- **Invalidation:** an `invalidation` evolution event carries the stored reason.

## 3. Multi-hop traversal and paths (M3, M4)

`GET /graph/paths` finds all shortest paths between two entities.

- **Algorithm:** bidirectional breadth-first search from Python, one bounded
  query per layer (`EXPAND_FRONTIER_WITH_PARENTS`). Each round expands the
  side whose frontier has the smaller total degree (`FRONTIER_DEGREE`),
  stops at the first layer where the two searches meet, and reconstructs all
  shortest chains through the meeting entities (capped, sorted).
- **Why bidirectional:** the one-sided search exhausted the 150-node budget
  before reaching a target on any graph with hubs at 1,000+ entities.
  Measured on the synthetic hub graph [M] (`eval/graph_intelligence_benchmark.py`):

  | Entities | Path found, one-sided | Path found, degree-guided bidirectional |
  |---|---|---|
  | 500 | budget exhausted (hub source) | yes, 7 entities explored |
  | 1,000 | budget exhausted | yes, 8 explored |
  | 10,000 | budget exhausted | yes, 149 explored (`truncated: true`) |
  | 5,000, hub → ordinary | budget exhausted | **still budget exhausted**, reported as such |

- **Bounds:** `GRAPH_PATH_MAX_HOPS` 3 (hard 4), `GRAPH_PATH_MAX_PATHS` 5
  (hard 20), and every visited entity on both sides counts against
  `GRAPH_EXPLORE_MAX_NODES` (150). A client may lower these, never raise them.
- **Cycle safety:** a search never revisits an entity on its own side, and
  every returned path has distinct entities [V: `test_a_cycle_...`].
- **Timeout:** there is no wall-clock timeout on a path query; the node budget
  and hop cap bound the work [L].
- **Each step** is the stored relationship(s) between two adjacent path
  entities, in either stored direction (`direction: forward|backward`), with
  predicate, `relation_kind`, status, `temporal_state`, validity window,
  `assertion_count`, `confidence`, the asserting `memory_ids` and a per-step
  `basis`.
- **Path `basis`:** `directly_stated` for a one-step path,
  `inferred_from_graph` for anything longer [V].
- **Ranking (heuristic, explained):**
  `0.4/length + 0.2·min(min_support,3)/3 + 0.25·current_fraction + 0.15·min_confidence`,
  returned with its four signals. The weights were chosen by reasoning, not
  fitted; there is no evidence for them beyond the evaluation below [L].
- **Empty results say why:** `same_entity`, `not_connected_within_hops`,
  `node_budget_exhausted`. The last is *not* proof that no connection exists.

## 4. Relationship explanations (M11)

`GET /graph/edges/explain` (`ExplainPanel` in the UI):

- `basis` (see API.md), `relation_kind`, aggregated validity window, best
  stated confidence, the supporting memories (each with its resolved
  sources), and a `why` sentence assembled **only** from stored windows,
  statuses and `SUPERSEDES` / `CONFLICTS_WITH` links, for example
  "Current: stated by 2 memories, valid from 2026-01-01; no end recorded." or
  "No longer true: stated by 1 memory, ended 2026-05-01 (status superseded);
  replaced by 1 later memory."
- An explanation exists only if the relationship exists in the requested scope
  (else 404), so it can never describe something that is not there.

## 5. Entity context (M5)

`GET /entities/{id}/context` returns, bounded and deterministic: the canonical
entity, aliases, merged-from names, current and historical relationships
(each labelled with its `relation_kind`), ranked related entities, mentioning
memories with exact totals (including how many are disputed), a began/ended
timeline and source-type counts.

Related-entity ranking is heuristic:
`0.5·currently_related + 0.35·min(support,5)/5 + 0.15·min(distinct_predicates,3)/3`.

## 6. Decision and causal relationships (M7)

What is built is **safe surfacing**, not extraction:

- Predicates are grouped into `causal` (a fixed set of stated cause/consequence
  predicates), `possible_cause` (stated as uncertain) and `association`
  (everything else). The label is shown on every relationship in paths,
  contexts and the UI, with the sentence "two things happening one after the
  other is never treated as cause and effect".
- **Not built:** nothing teaches extraction to *produce* `led_to`, `fixed_by`
  or `possible_cause` edges. The extraction prompt does not mention
  predicates, and changing it needs real-LLM evidence (ADR 0016). Until then
  such edges exist only if a user or an API caller stated them. This is the
  natural place for Phase 16, where a coding session states decisions and
  fixes explicitly [L].
- **Observed sequence** is never represented as a relationship. The evaluation
  includes a case where two events follow each other with no stated cause and
  the path between them contains no causal step [V: Q1].

## 7. Memory evolution (M6)

`GET /memories/{id}/evolution` walks the memory's bounded history chain
(supersedes / superseded-by / duplicates / conflicts, at most 20 memories)
and returns typed events in chronological order: `new_fact`, `supersession`,
`conflict`, `conflict_resolved`, `invalidation`, `merge`, `reconfirmation`.

- Each event comes from a stored record: a memory, a link, an extraction
  decision (`extraction_candidates`) or a review decision (`review_audit_log`).
- Original evidence is preserved: superseded memories keep their content,
  windows and sources, and appear in the chain.
- **No `correction` kind.** The stored model has no marker separating a
  correction from any other supersession, and inventing one would be
  fabrication [L].
- `current_memory_ids` are the chain members believed and valid now.

## 8. Ranking, retrieval and context subgraphs (M8, M9, M10)

**Retrieval (M9).** Hybrid retrieval's graph channel now also matches an
entity's **aliases** (whole-token, as names), so a query naming a merged
entity's old spelling anchors on the canonical entity. The mention detector is
one public method shared with the context builder. Phase 3's evaluation output
is byte-identical to before [V].

**Context subgraph (M10)**, `POST /graph/context`:

1. Hybrid retrieval returns evidence memories (temporal intent from the
   explicit scope, else the Phase 14 text rules).
2. **Anchors** = entities the query names (name or alias), resolved through
   merges (at most 4).
3. **Contributing evidence** = memories that matched on content: a keyword
   match, or a vector match with similarity ≥
   `GRAPH_CONTEXT_MIN_VECTOR_SIMILARITY` (0.5). Memories found only because
   they mention an anchor are neighbours, not answers: they stay in the
   returned `evidence` list (nothing is hidden) but do not add entities. If
   nothing matched on content, the top three are used.
4. Shortest paths between anchors are added (one path per anchor pair).
5. A stored relationship is kept only if a contributing memory asserts it or
   it lies on an anchor path. **This is what removes irrelevant neighbours.**
6. Nodes are scored, sorted and cut to `GRAPH_CONTEXT_MAX_NODES`; every score
   comes with its signals.

Heuristic scores (returned with their inputs):

| Element | Formula |
|---|---|
| Edge | `0.35·asserted_by_evidence + 0.25·on_anchor_path + 0.15·touches_anchor + 0.15·current + 0.10·support` |
| Node | `0.40·anchor + 0.30·mentioned_by_evidence + 0.20·on_anchor_path + 0.10·kept_degree` |
| Evidence memory | the hybrid retrieval score and its channel signals (Phase 3, unchanged) |

**Calibration caveat [L].** The 0.5 similarity threshold was tuned against the
same dataset it is scored on (one task failed until it was added), using the
deterministic fake embeddings, which are a hashed bag of words. It is a
heuristic to recalibrate for any real embedding model. The context results are
therefore in-sample.

## 9. Graph Explorer (M12, M13)

All in the existing explorer (`components/graph/`), which keeps its list view
as the full keyboard/screen-reader alternative:

- **Temporal mode:** "Only no-longer-true facts" (`historical`) beside the
  existing "Include no-longer-true facts (labelled)" (`all`) and the time lens.
- **Filters:** entity type (server-side, used by overview, neighbourhood and
  every expansion); relationship type (client-side visibility over what is
  loaded; nothing the server returned is dropped).
- **Find path to…:** pick a second entity by name or alias; the panel lists
  paths with per-step predicate, direction, kind, temporal state and memory
  links, the basis label, and an expandable note that the ranking is
  heuristic. "Show on canvas" adds any missing path entities and edges
  (existing nodes keep their data) and highlights the path (thicker and
  accent-coloured, everything else faded; the textual path is the non-colour
  equivalent).
- **Relationship explanation** above the evidence for a selected edge.
- **Collapse neighbours** undoes one expansion (removes only what it added,
  and their edges).
- **Elsewhere:** the entity page gains "At a glance" (§5) and the memory page
  "How this knowledge evolved" (§7).
- **Bounded:** no new endpoint returns more than the caps above; the canvas
  cap (600 elements) and truncation banners are unchanged.
- **Not built:** a graph-page search box for the *focus* (entity search exists
  on `/entities`), a date-range or confidence-threshold filter, and a source
  type filter [L]. Canvas keyboard navigation is still absent (the list view
  is the alternative) [L].

## 10. Performance and limits (M14) [M]

Local, one Neo4j Community instance, synthetic data from Phase 4
(`eval/graph_intelligence_benchmark.py`). Medians of 5, in-process; the
machine was shared and load varied, so read them as orders of magnitude.

| Entities (edges) | paths, ordinary | context, hub | neighbourhood, hub, 2 hops (current) | context subgraph |
|---|---|---|---|---|
| 100 (233) | 30 ms | 17 ms | 31 ms | 210–430 ms |
| 500 (1.5k) | 17–28 ms | 15 ms | 46 ms | 700–1,270 ms |
| 1,000 (4k) | 45–67 ms | 15 ms | 35 ms | 520–660 ms |
| 5,000 (60k) | 19–48 ms | 27 ms | 46 ms | 510–1,330 ms |
| 10,000 (220k) | 23–109 ms | 43 ms | 89 ms | 830–1,810 ms |

- **Payloads:** a hub's entity context is about 12 KB, a path about 1–6 KB, an
  explanation about 0.5–1 KB, a context subgraph about 8–11 KB. The 2-hop hub
  neighbourhood (Phase 8, unchanged) is ~120–145 KB at the 150-node budget.
- **The context subgraph is dominated by retrieval.** For the same query the
  plain hybrid search takes 446–1,734 ms across these sizes, so the context
  builder adds little on top. The retrieval time is the pre-existing graph
  channel walking a hub's neighbourhood (isolated: ~1.26 s of a 1.30 s search
  at 10,000 entities, of which the keyword channel is ~36 ms) [M].
  This was not optimised in this phase [L].
- **My Phase 14 term-keyword query is not the bottleneck:** 13 ms at 10,000
  memories [M]. (Phase 14 had labelled that an assumption.)
- **Hard limits stay enforced** (tests: `test_paths_respect_the_hop_cap`,
  `test_context_query_length_and_budget_are_validated`, the node-budget tests).
- **Frontend layout** is unchanged (Phase 10/11: ~1.5–2.2 s at 597 elements
  under cose); new features add at most a handful of elements (a path adds its
  entities and edges).

## 11. Integrity and isolation (M15) [V]

- Every new Cypher constant is in the audited `USER_SCOPED_STATEMENTS` and
  passes the structural checks: user-scoped nodes and edges, bounded, read
  only, instant-based temporal comparisons.
- **New guard:** every audited statement (51) is planned under **both Cypher 5
  and Cypher 25** by `test_cypher_dialects.py` (AuraDB defaults to Cypher 5).
  It checks parsing, not behavioural equivalence on AuraDB itself [L].
- **Cross-user:** user B gets 404 for user A's entities, memories, edges and
  historical scope on every new endpoint, and an empty (not leaking) result
  from the context endpoint. Verified in `test_another_user_gets_404_everywhere`
  and eval task X1.
- **Merged entities:** paths and contexts resolve redirects on both ends.
- **No raw Cypher/SQL reaches a client; no client value reaches query text**
  (parameters only; the hop count of the older template is validated and
  formatted, unchanged).
- **Not covered:** project/context scoping below the user does not exist yet
  (Phase 16).

## 12. Evaluation (M16) [V]

`eval/graph_intelligence_runner.py`, dataset
`eval/datasets/graph_intelligence_v1.json`: 28 tasks in categories A–Q plus a
cross-user probe, over a synthetic world (24 memories, a merged entity, a
200-spoke hub, a second user). It calls the real routes through the ASGI app;
no LLM is involved. It reports pass/fail per task, path precision,
context include-recall and exclusion-rejection, latency and payload size, and
is deterministic (two runs identical apart from latency/bytes). Dataset
structure is guarded by `test_graph_intelligence_dataset.py`.

**Honest caveats [L]:** the dataset was written together with the code it
scores, so it is optimistic; tasks are one or two per category; and the
context threshold is in-sample (§8). It is a regression net, not an accuracy
benchmark.

## 13. Real-LLM evaluation (M17)

**Not applicable, and not run.** No new endpoint calls an LLM. Retrieval uses
the configured embedding provider (deterministic fake here). No provider
credentials, quota or specific vendor are involved.

## 14. Phase 16 extension points

Phase 15 does **not** implement MCP or any coding-agent integration. What it
gives Phase 16:

| Phase 16 question | Phase 15 primitive |
|---|---|
| "Where did I stop / what is the latest known state?" | `GET /entities/{id}/context` on the project, task or session entity: current relationships, began/ended timeline, disputed count |
| "What did I try before / how did this change?" | `GET /memories/{id}/evolution` and `historical` scope |
| "Why did I make this decision?" | `paths` and `relation_kind`: only stated causes are labelled causal |
| "Which files / errors / fixes relate to this?" | `POST /graph/context`: the smallest subgraph plus its evidence and sources |
| "What is the evidence for this?" | `GET /graph/edges/explain` with resolved sources |

**Already representable with no model change** (existing entity types and
free predicates): Project (`project`), Decision (`decision`), and Bug / Error /
Fix / Change / AgentSession / Commit as `event` or `task`; predicates
`contains`, `changed`, `modified`, `worked_on`, `made`, `led_to`, `fixed_by`,
`depends_on`, `superseded_by`.

**Extensions Phase 16 needs (designed in `docs/PHASE12_PLAN.md`):**
dedicated entity types (`file`, `commit`, `agent_session`, ...) with exact
prefixed identity, so `auth/session.py` never fuzzy-matches; a project scope
(a memory space per project); structural, non-LLM ingestion of commits and
session events; source types `git_commit` and `coding_session` with provenance
resolvers; and an ordering/"unfinished" notion for sessions, which the current
model does not have (a task's status is only the memory lifecycle status).

**Not done in this phase:** exposing these primitives as agent tools. That
changes the agent's tool set and prompt, which needs real-LLM evidence
(ADR 0016), and it belongs with the MCP adapter.
