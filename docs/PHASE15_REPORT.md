# Phase 15 — Advanced Memory Graph & Graph Intelligence: Report (2026-09-30)

**Scope.** Bounded, explainable graph reasoning over the existing memory graph:
historical scope, paths, relationship explanations, entity context, memory
evolution, context subgraphs and Explorer improvements.
- No deployment, no infrastructure work, no new dependency.
- **No Phase 16 work** (MCP / coding agent). Extension points only (§17).
- **Baseline:** `2ecb83f`. The audit is `docs/PHASE15_GRAPH_MODEL.md`; the
  as-built design is `docs/PHASE15_GRAPH_INTELLIGENCE.md`; the endpoint
  reference is `docs/API.md` §4d; the decision is ADR 0033.

**Labels:** [V] verified by a test or check that ran; [M] measured;
[I] inspected in code; [A] assumption; [L] limitation.

**Conditions and non-claims.**
- Everything is local: Apple M1, PostgreSQL 17, Neo4j Community 2026.08.1.
- Embeddings are the deterministic fake; no LLM is involved in any new path.
- **No real-LLM result exists for this phase** (§15).
- **This is not a production-readiness claim.** The system is not deployed.
- The machine was shared and load varied, so latencies are orders of magnitude.

## 1. Executive summary

Five read-only endpoints and one scope were added, plus Explorer, entity-page
and memory-page UI. The graph schema, PostgreSQL schema and Neo4j indexes are
unchanged [V: `alembic check` reports no new operations]. Nothing inferred is
stored. The deterministic evaluation passes 28/28 tasks, the full backend
suite 1679 passed, frontend unit 86 passed, E2E 39 passed. The evaluation
dataset was written alongside the code, so it is a regression net and not an
accuracy benchmark (§14, §16).

## 2. Existing graph (audit, M0) [I]

Three node labels (`Memory`, `Entity`, `Source`) and seven relationship types
(`MENTIONS`, `DERIVED_FROM`, `RELATES_TO`, `SUPERSEDES`, `CONFLICTS_WITH`,
`DUPLICATE_OF`, `MERGED_INTO`). A `RELATES_TO` edge carries a free-text
predicate and the `memory_id` that asserted it. Validity is half-open
`[valid_from, valid_until)`. Full audit: `PHASE15_GRAPH_MODEL.md`.

## 3. Gaps found

| Gap | Consequence |
|---|---|
| No way to ask only for facts that stopped being true | "What did I use before?" needed client-side filtering of the `all` scope |
| Phase 8 neighbourhood only expands from a root | No "how are A and B connected?" |
| An edge shows a predicate and evidence only | No statement of why it is shown, how strongly or whether it is current |
| Entity page lists relationships only | No current/historical split or timeline |
| Memory page shows links only | Evolution had to be pieced together by hand |
| Retrieval's graph channel ignored aliases | A merged entity's old spelling did not anchor |
| One-sided path search on hubs | Exhausted the node budget before reaching the target (§9) |

## 4. Changes made

| # | Change | Where |
|---|---|---|
| 1 | `historical` temporal scope | `graph_repository.py`, `schemas/graph.py` |
| 2 | 7 new audited Cypher statements + 6 repository methods | `graph_repository.py` |
| 3 | `GraphIntelligenceService` | `services/graph_intelligence_service.py` |
| 4 | Schemas | `schemas/graph_intelligence.py` |
| 5 | `GET /graph/paths`, `GET /graph/edges/explain`, `POST /graph/context` | `routes/graph.py` |
| 6 | `GET /entities/{id}/context` | `routes/entities.py` |
| 7 | `GET /memories/{id}/evolution` | `routes/memories.py` |
| 8 | Alias-aware graph channel (`mentioned_entities` made public) | `hybrid_retrieval_service.py` |
| 9 | 8 bounded settings (caps lowerable, never raisable) | `core/config.py` |
| 10 | Explorer: temporal mode, entity-type and relationship filters, path finder, explanation panel, collapse | `frontend/components/graph/*` |
| 11 | "At a glance" on the entity page, "How this knowledge evolved" on the memory page | `EntityContextPanel`, `EvolutionPanel` |
| 12 | Evaluation runner, dataset, benchmark | `backend/eval/` |
| 13 | Cypher 5 / Cypher 25 planning guard for all audited statements | `tests/integration/test_cypher_dialects.py` |

## 5. Graph model and semantics [V]

No new labels, relationship types or properties. `relation_kind`
(`causal` / `possible_cause` / `association`) is derived from the stored
predicate only, and `basis` (`directly_stated`, `supported_by_multiple_memories`,
`inferred_from_graph`, `temporally_derived`) from how a result was obtained.
See `PHASE15_GRAPH_INTELLIGENCE.md` §1, §6.

## 6. Temporal graph intelligence [V]

`historical` = no longer true at the reference instant (closed window or a
superseded/historical status); excluded statuses stay excluded. It is one
predicate inside the shared scope fragments, so it applies on every hop before
any LIMIT. Verified by eval tasks C1, D1, O1 and the integration tests. The
other scopes are unchanged: response bodies of the existing Phase 8 endpoints
are byte-identical to the baseline and latency is within ±3% [M].

## 7. Multi-hop and path intelligence [V][M]

Bidirectional, degree-guided breadth-first search, bounded by
`graph_explore_max_nodes` (150), hops (default 3, hard 4) and paths (default 5,
hard 20). All shortest paths are returned with per-step stored relationships,
a heuristic score with its four signals and an explicit reason when empty.
Cycle-safe [V]. A path of more than one step is labelled
`inferred_from_graph`. Timings are in §12.

## 8. Decision and causal semantics [V][L]

Causal labels appear only where the stored predicate says so. Sequence is never
treated as cause (eval Q1). **Limitation:** nothing teaches extraction to
produce causal predicates; the prompt is unchanged (ADR 0016 requires real-LLM
evidence for that).

## 9. Hub and scale behaviour [M]

The one-sided search exhausted the budget on hub graphs from 500 entities up.
Degree-guided bidirectional search found a path at 500, 1,000 and 10,000
entities. **At 5,000 entities a hub → ordinary path still exhausts the node
budget** and is reported as `node_budget_exhausted` (not as "no connection").
Unresolved; a larger budget would breach the Phase 8 response bounds.

## 10. Entity context and evolution [V]

Entity context: canonical entity, aliases, merged-from names, current and
historical relationships, ranked related entities, mentioning memories with
exact totals, timeline. Evolution: typed events (`new_fact`, `supersession`,
`conflict`, `conflict_resolved`, `invalidation`, `merge`, `reconfirmation`),
each from a stored record. **There is no `correction` kind**: the stored model
has no marker for it [L].

## 11. Context subgraphs and retrieval [V][L]

`POST /graph/context`: hybrid retrieval, anchors from entity names/aliases,
contributing evidence (keyword or vector match ≥ 0.5), anchor-pair paths, and
a relationship kept only if a contributing memory asserts it or it lies on an
anchor path. Irrelevant neighbours are rejected (eval K1, rejection 1.00).
**The 0.5 similarity threshold was tuned on the same dataset it is scored on,
using the deterministic fake embeddings.** The context results are in-sample.

## 12. Performance [M]

Synthetic Phase 4 graph, real routes in-process, median of 5. Details and the
payload sizes are in `PHASE15_GRAPH_INTELLIGENCE.md` §10.

| Entities (edges) | paths, ordinary | entity context, hub | context subgraph |
|---|---|---|---|
| 100 (233) | 30 ms | 17 ms | 210–430 ms |
| 500 (1.5k) | 17–28 ms | 15 ms | 700–1,270 ms |
| 1,000 (4k) | 45–67 ms | 15 ms | 520–660 ms |
| 5,000 (60k) | 19–48 ms | 27 ms | 510–1,330 ms |
| 10,000 (220k) | 23–109 ms | 43 ms | 830–1,810 ms |

The context subgraph is dominated by the pre-existing hybrid search's graph
channel on hub entities (about 1.26 s of a 1.30 s search at 10,000 entities).
**Not optimised in this phase** [L]. Ranges span runs under varying load.

## 13. Explorer UI [V]

Path finder, relationship explanation, filters, collapse, entity-page and
memory-page panels. 7 new E2E tests (5 graph, 2 accessibility, axe: no serious
violations) and 86 unit tests. Two fixes were needed along the way: duplicate
UI elements, and a navigation-prefetch request storm from `Link` components in
the new panels (fixed with `prefetch={false}`). One accessibility test waited
for `networkidle`, which the layout's own prefetches can keep pending; it now
waits for the context response. **Not built:** a focus-search box on the graph
page, date-range / confidence / source-type filters, canvas keyboard
navigation (the list view remains the alternative) [L].

## 14. Evaluation [V][L]

`eval/graph_intelligence_runner.py`: 28 tasks over categories A–Q plus a
cross-user probe; deterministic; no LLM.

| Result | Value |
|---|---|
| Tasks passed | 28/28 |
| Path precision | 1.0 |
| Context include-recall / exclusion-rejection | 1.00 / 1.00 on every context task |
| Median latency | context 110 ms, entity context 28 ms, evolution 24 ms, explain 10 ms, neighbourhood 12 ms, paths 15 ms |
| Median payload | context 9.2 KB, entity context 3.6 KB, explain 1.0 KB, paths 1.2 KB |

Also re-run and passing: graph API conformance 39/39, retrieval, document,
agent and memory-quality evaluations. **Caveat:** one or two tasks per
category, written with the code they score.

## 15. Real-LLM evaluation

Not run and not applicable: Phase 15 adds no LLM code path. No provider,
credential or quota was used, and no result is claimed.

## 16. Tests and verification [V]

| Check | Result |
|---|---|
| Backend suite | **1679 passed** |
| New backend tests | 144 (19 integration, 103 Cypher dialect, 16 unit, 6 dataset) |
| Frontend typecheck / lint | clean |
| Frontend unit | **86 passed** (10 files) |
| E2E (production build, real stack) | **39 passed** |
| `ruff check app tests eval ../scripts` | clean |
| Secret scan | 289 files, 0 findings |
| `alembic check` | no new upgrade operations |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `pip-audit` | **not run**: not installed in the environment |
| `ruff format --check` | 61 files would be reformatted; not enforced by CI and not applied, to avoid unrelated churn |

**Security and isolation.** Every new Cypher constant filters on `$user_id`
and passes the structural audit; a foreign id is a 404 on every new endpoint
(eval X1, integration test) [V]. Only parameters reach queries. No secret,
token or private data appears in any fixture, log or document [V: secret scan].

**Cypher dialects.** All 51 audited statements plan under Cypher 5 and Cypher
25. This checks parsing, not behaviour on AuraDB [L].

## 17. Phase 16 readiness

Not implemented. Extension points and the mapping from "what was I doing?"
style questions to the new primitives are in
`PHASE15_GRAPH_INTELLIGENCE.md` §14. **Phase 16 still needs:** dedicated
entity types (file, commit, agent session) with exact identity, a project
scope, structural non-LLM ingestion, session ordering and "unfinished" state,
agent tools over these endpoints, and causal extraction with real-LLM evidence.

## 18. Limitations

- Context threshold and all ranking weights are unfitted heuristics; the
  threshold is in-sample on fake embeddings.
- The evaluation dataset is small and authored with the code.
- Path search at 5,000 entities (hub → ordinary) still exhausts the budget.
- The graph channel of hybrid retrieval is slow on very large hubs
  (pre-existing; not decomposed further).
- No `correction` event kind; no causal extraction; agent tools not extended.
- Cypher check is parse-only for AuraDB; no real-AuraDB run.
- No wall-clock timeout on path queries (node/hop budgets only).
- No PostgreSQL migration and no new Neo4j index, so any index benefit for
  the new statements is unmeasured.
- Prefetch-storm fix verified by before/after request counts, and E2E only.
- Leftover data: the existing eval runners leave small `eval-agent-*` graphs
  (about 480 nodes) in the local dev graph; not created by Phase 15 code and
  not deleted.

## 19. Commit

One commit, `feat: improve memory graph intelligence`, on top of `2ecb83f`.
Nothing pushed; no remote configured.
