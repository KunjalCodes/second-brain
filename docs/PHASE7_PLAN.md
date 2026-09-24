# Phase 7 Plan — Unified Evaluation Framework & Agent Retrieval-Reliability

**Status:** Proposed, awaiting approval. Planning only — no code, dependencies,
migrations, or database changes have been made as part of producing this
document.

---

## 0. How this plan was produced

Before drafting a recommendation, the following were inspected directly:
`docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/EVALUATION.md`, `docs/API.md`,
`docs/PHASE2_PLAN.md` through `docs/PHASE6_PLAN.md`, `docs/PHASE2_HARDENING.md`,
every ADR (0001–0014), the root `README.md`, `frontend/`, `scripts/`,
`.github/` workflows, `backend/.env.example`, the current API route modules
(`backend/app/api/routes/*.py`), the current PostgreSQL models and every
migration (`backend/app/db/models.py`, `backend/migrations/versions/`), the
Neo4j schema (`backend/app/graph/schema.py`), the extraction pipeline
(`backend/app/extraction/`), the document ingestion pipeline
(`backend/app/documents/`), the retrieval services (`backend/app/services/
retrieval_service.py`, `hybrid_retrieval_service.py`), the agent
(`backend/app/agent/`, `backend/app/services/agent_service.py`), and every
evaluation harness under `backend/eval/`.

### 0.1 What that inspection found (the load-bearing facts for this plan)

- **The project's own roadmap (root `README.md`) already designates Phase 7
  as "Evaluation framework (answer-quality/citation/agent metrics)."**
  This plan follows that existing sequencing rather than jumping ahead to
  Phase 8 (authentication) or Phase 9 (deployment/UI) items, several of
  which are explicitly gated on auth landing first (ADR 0005: "This is not
  authentication... the backend must not be exposed to untrusted callers
  before Phase 8").
- **That README table is stale**: it still shows Phase 6 as "Not started"
  and lists ADRs "0001–0008," even though Phase 6 is complete (committed
  `60f4d599`, 736 tests passing, ADRs through 0014). `docs/EVALUATION.md`'s
  own implementation-status header is similarly stale — it says agent/
  citation metrics are "not yet measurable... out of scope until Phase 5,"
  but Phase 5's `eval/agent_runner.py` already measures exactly that, and
  Phase 6's `eval/document_runner.py` extends it to documents. **Phase 7 is
  therefore not "build an evaluation framework from nothing" — a real one
  already exists.** The actual gap is narrower: the existing framework is
  fragmented across six independent scripts with no shared history/
  regression tracking, and several specific, already-documented measurement
  gaps were deliberately left open rather than patched.
- **Frontend is not a partial scaffold — it is two files** (`frontend/
  README.md`, `frontend/.env.example`), zero application code. The
  project's own roadmap places UI work at Phase 9, after authentication
  (Phase 8). Building a UI now would mean building on nothing, ahead of
  auth, contrary to the project's own stated sequencing — see §10.
- **Memory consolidation/decay is explicitly named as future, unscheduled
  work** in ADR 0002 ("consolidation is listed as a future job in
  ARCHITECTURE.md section 17"), not yet claimed by any phase.
- **Observability is aspirational only** (`ARCHITECTURE.md` §22 has no
  "Implemented" tag; no structured logging, tracing, or metrics library is
  wired in beyond Python's stdlib `logging`).
- No authentication beyond the Phase 1 `X-User-Id` placeholder (ADR 0005),
  no rate limiting, no CI job for a nonexistent frontend.

This inspection is why the candidate list below is evaluated against what
is *actually* true of the repository today, not against assumptions.

---

## 1. Candidate capabilities considered

Per the brief's own list, every candidate was considered; four were judged
serious enough (concrete, evidenced by the actual codebase, right-sized for
one phase) for the full architectural review in §2:

1. **Unified evaluation framework + agent retrieval-reliability** (recommended — §2.1)
2. **Memory consolidation / lifecycle (decay, archival sweep)** (§2.2)
3. **Observability** (§2.3)
4. **Entity resolution improvements** (§2.4)

The remaining items from the brief's list were considered and set aside for
the reasons given in §9 (Deferred): advanced conflict resolution (the
decision engine already handles CONFLICTING/SUPERSEDING/DUPLICATE/
LIKELY_DUPLICATE/FLAGGED with no evidenced gap — see ADR 0007/0009/0010, no
phase plan names an open conflict-resolution defect); memory write
governance (ADR 0011's reuse of the extraction pipeline for agent writes
already gives writes the identical grounding/conservative-policy treatment
as passive extraction — no evidenced gap); user-facing memory inspection/
review UI, document management UI, frontend memory explorer (all blocked on
Phase 8 auth and Phase 9 UI per the existing roadmap, and there is nothing
to extend — see §10); production hardening, authentication/multi-user,
deployment architecture (explicitly Phase 8/9 per the roadmap, and
premature without the evaluation groundwork this phase proposes);
performance/scaling (ARCHITECTURE.md §29: "the first version should
optimize for correctness rather than massive scale... revisited only when a
measured latency or reliability problem appears" — no such problem is
currently measured); memory decay/importance (folded into candidate 2,
§2.2, and deferred for the same reasons); richer temporal reasoning
(Phase 4 already built bi-temporal validity, multi-hop decay, and `as_of`
queries; Phase 6 extended this to document-sourced facts and it is
evaluated — no evidenced gap remains that isn't already covered by
candidate 1's expanded temporal task coverage).

---

## 2. Architectural review of serious candidates

### 2.1 Unified evaluation framework + agent retrieval-reliability (recommended)

1. **What problem does it solve?** Six independent, non-communicating
   evaluation scripts (`eval/runner.py`, `retrieval_runner.py`,
   `duplicate_threshold_calibration.py`, `graph_scale_benchmark.py`,
   `agent_runner.py`, `document_runner.py`) each print results to stdout
   and get manually transcribed into phase-plan markdown. There is no way
   to compare a run against a previous run, detect a regression
   automatically, or answer "is the system getting better or worse over
   time?" without a human re-reading old markdown tables. Two specific,
   already-documented measurement gaps remain open: the Phase 5
   `ambiguous_entity` task (real Gemini made zero tool calls, 0% recall,
   explicitly left unfixed pending "a future evaluation expansion") is a
   single anecdote, not a measured category; and Phase 6's real-Gemini
   document evaluation completed only 1/5 tasks before hitting free-tier
   `HTTP 429`, with no mechanism to resume a quota-limited run across
   sessions without re-spending already-consumed quota on tasks that
   already succeeded.
2. **Why is it needed after Phase 1–6?** Every phase so far built a new
   memory capability and measured it once, by hand, at the end. Phase 1–6
   collectively built enough capability (extraction, retrieval, temporal
   reasoning, review, agent tools, document ingestion) that the project's
   own stated central question — "does structured persistent memory
   actually help, and when does it not?" (`EVALUATION.md` §22) — can now be
   asked *across* phases, not just within one. That requires a shared,
   persistent record of results, which does not exist yet.
3. **Which existing limitation does it address?** Directly, all four the
   user's brief calls out: the Phase 6 quota-limited real-Gemini coverage,
   the partial document real-LLM validation, the minimal baseline B, and
   the Phase 5 `ambiguous_entity` failure — see §4 for how each is
   addressed, not silently worked around.
4. **Which existing components can be reused?** All of them, and reuse is
   the point: `eval/metrics.py`'s `ScenarioResult`/`EvaluationReport`/
   `build_report` (extended, not replaced); every existing harness's own
   seeding/checking logic (`_seed_memories`, `_seed_documents`,
   `_build_document_candidate`, etc.) unchanged; `AgentService`,
   `HybridRetrievalService`, `ExtractionRunner`, `ProvenanceService`
   unchanged; `app.llm.factory.build_llm_client` (the one real-LLM entry
   point every harness already uses) unchanged; `app.agent.prompts.
   SYSTEM_PROMPT`'s existing structure (a conditional, evidence-gated
   addition, not a rewrite — see §7.5).
5. **What new components would be required?** (a) A thin report aggregator
   (`eval/report.py`) that runs or ingests each harness's existing result
   objects and writes one consolidated report plus a persisted row per run;
   (b) one new, small PostgreSQL table, `evaluation_runs`, to make results
   queryable/comparable over time (§6); (c) a quota-aware checkpoint file
   for real-LLM runs, so a rate-limited sweep resumes where it left off
   instead of re-spending quota on already-completed tasks; (d) an expanded
   agent-evaluation dataset generalizing `ambiguous_entity` into a real
   task *category* (several tasks, not one); (e) a deeper baseline B in
   `eval/document_runner.py` (more tasks, `recall@k` not just top-1 hit).
6. **Does it affect Neo4j?** No. Nothing here writes to or reads canonical
   memory outside of what the existing harnesses already do (they seed and
   query memories through the unchanged `MemoryService`/
   `HybridRetrievalService`).
7. **Does it affect PostgreSQL?** Yes, additively: one new table
   (`evaluation_runs`, §6). No existing table's schema changes.
8. **Does it affect the agent?** Only conditionally and narrowly: *if* the
   expanded retrieval-necessity dataset shows a systematic (not one-off)
   gap, one general-purpose instruction is added to `SYSTEM_PROMPT` (never
   naming a specific entity or task, matching the file's own existing "never
   task-specific" discipline), and the improvement is verified by re-running
   the same dataset (an ablation, before/after, per §5). If the dataset
   does not show a systematic gap, no agent code changes at all — a
   negative result is reported, exactly as Phase 5 already did for this
   same finding.
9. **Does it affect retrieval?** No changes to `RetrievalService` or
   `HybridRetrievalService`. Only their existing evaluation coverage
   deepens.
10. **Does it affect document ingestion?** No pipeline changes. Only
    `eval/document_runner.py`'s own evaluation depth changes (baseline B,
    resumable real-LLM section).
11. **Does it affect evaluation?** This candidate *is* the evaluation
    change — see §5 for the full before/target/metrics design.
12. **What new dependencies would it require?** None. Everything needed
    (report generation, a checkpoint file, one Postgres table) is buildable
    with the stack already in the project (stdlib, SQLAlchemy/Alembic,
    Pydantic).
13. **Can it remain zero-cost?** Yes, directly: the deterministic sections
    of every harness already run with `FakeLLMClient`/`FakeEmbeddingClient`
    at zero cost; the real-LLM sections are opportunistic and quota-aware,
    exactly like Phase 5/6 already are, with the checkpoint mechanism
    making better use of a free tier's limited quota rather than
    requiring more of it.
14. **Implementation risk?** Low-to-moderate. The main risk is scope creep
    into "redesign the whole eval system" — mitigated by the explicit scope
    boundary in §9 (extend, do not rewrite, the six existing harnesses).
    A secondary risk is the conditional prompt change regressing another
    agent-evaluation category — mitigated by requiring the full existing
    `agent_v1.jsonl` suite (not just the new category) to be re-run and
    shown non-regressing before the change is accepted (§5).
15. **How would success be measured?** (a) One command produces one
    consolidated, historized evaluation report covering all six existing
    harnesses; (b) the retrieval-necessity category has ≥5 tasks (not 1)
    with a measured recall, and the result — improved or still imperfect —
    is reported honestly, not smoothed over; (c) baseline B covers every
    applicable document task, not 2; (d) a documented, tested procedure
    exists for resuming a rate-limited real-LLM run without re-spending
    quota on completed tasks; (e) all 736 existing tests still pass, plus
    new tests for the report aggregator and the `evaluation_runs`
    repository.

### 2.2 Memory consolidation / lifecycle (decay, archival sweep)

1. **What problem does it solve?** Nothing is ever deleted (ADR 0002, by
   design — bi-temporal, append-only); storage grows monotonically forever.
   A consolidation/decay job would periodically archive or summarize very
   old, low-relevance memories.
2. **Why is it needed after Phase 1–6?** It isn't clearly needed *yet* —
   it's needed at scale, and no phase has measured a storage or retrieval-
   quality problem caused by unconsolidated history (`ARCHITECTURE.md`
   §29's own stated posture: revisit "only when a measured latency or
   reliability problem appears").
3. **Which existing limitation does it address?** ADR 0002's own forward
   note: "consolidation is listed as a future job... not yet scheduled."
4. **Which existing components can be reused?** `MemoryStatus.ARCHIVED`
   already exists as a manual, user-driven status value (`app/models/
   lifecycle.py`'s transition table already permits `ACTIVE → ARCHIVED`);
   the review-sweep pattern (`app/services/review_reconciliation_service.py`,
   `app/extraction/trigger.py`'s periodic sweep) is a ready-made template
   for a bounded, idempotent background job.
5. **What new components would be required?** A scoring model (what makes
   a memory "consolidation-eligible"? recency, retrieval-frequency,
   confidence — none of these are currently tracked per-memory), a sweep
   job, and almost certainly a review step before anything is archived
   automatically (irreversible-feeling even though it's a soft status).
6. **Does it affect Neo4j?** Yes, directly — this is the highest-risk part
   of this candidate: it means writing to canonical memory on a schedule,
   not in response to a user/agent action.
7. **Does it affect PostgreSQL?** Yes — would need new columns or a new
   table to track retrieval-frequency/importance signals that do not exist
   today.
8. **Does it affect the agent?** Only indirectly (fewer/different memories
   retrievable).
9. **Does it affect retrieval?** Potentially significantly — archived
   memories are already excluded from default retrieval, so a decay policy
   changes *what a user can find*, which is a product-level decision, not
   only an engineering one.
10. **Does it affect document ingestion?** No.
11. **Does it affect evaluation?** Yes — would need a new evaluation
    category (did consolidation preserve retrievability of what matters?),
    itself a nontrivial design problem with no existing precedent to reuse.
12. **New dependencies?** None required, but a real scoring/decay function
    is a genuine, non-trivial algorithm-design task, not glue code.
13. **Zero-cost?** Yes, technically achievable with existing infrastructure.
14. **Implementation risk?** **High** relative to the other candidates: it
    is the only candidate that writes to canonical memory automatically,
    on a schedule, with no existing precedent for what "correct" looks
    like, and no measured evidence yet that it's needed. This is exactly
    the shape of premature, unjustified complexity `ARCHITECTURE.md` and
    `EVALUATION.md` repeatedly warn against elsewhere in the project's own
    history (e.g., Phase 4 explicitly declined to add a fan-out cap
    "without measured evidence").
15. **How would success be measured?** Unclear without first defining what
    "importance" means for this system — itself unresolved.

**Conclusion for this candidate:** genuinely on the roadmap eventually, but
premature for Phase 7 — no measured need, higher risk (canonical-memory
writes with no review precedent), and it depends on retrieval-quality
signals (e.g., "which memories does the agent actually cite/find useful?")
that candidate 2.1's expanded evaluation would itself help establish first.
Recommended as a strong Phase 8+ candidate, informed by what Phase 7's
evaluation work discovers about real usage patterns.

### 2.3 Observability

1. **What problem does it solve?** `ARCHITECTURE.md` §22 and `CLAUDE.md`
   §32 both specify a desired observability surface (`request_id`,
   `retrieval_strategy`, `tool_calls`, latency, token usage) that does not
   exist as structured, queryable data today — only ad-hoc Python
   `logging` calls and per-eval-run print statements.
2. **Why is it needed after Phase 1–6?** The system is now complex enough
   (extraction + retrieval + agent + document pipeline, each with its own
   failure modes) that debugging a production issue without structured
   traces would be difficult.
3. **Which existing limitation does it address?** `ARCHITECTURE.md` §22's
   own unimplemented wishlist.
4. **Which existing components can be reused?** `app/core/logging.py`
   (exists, minimal); every service already computes the fields
   `CLAUDE.md` asks for (latency, tokens, retrieved ids) — they're just not
   captured anywhere durable today.
5. **What new components would be required?** A structured logging/tracing
   layer, request-id propagation through every service call, and — to be
   genuinely useful rather than log-line noise — a place to query it (which
   starts to overlap with candidate 2.1's `evaluation_runs` idea, but for
   live traffic rather than eval runs).
6–10. Touches every layer (Neo4j client, Postgres client, agent, retrieval,
   ingestion) shallowly, rather than any one deeply.
11. **Does it affect evaluation?** Complementary, not a substitute — good
    observability would make debugging a *failed* evaluation task easier,
    but doesn't itself measure whether memory works.
12. **New dependencies?** Possibly a structured-logging library
    (`structlog` or similar) — evaluable against "is stdlib `logging` with
    a JSON formatter sufficient?" (it likely is, avoiding a new dependency).
13. **Zero-cost?** Yes — self-hosted/local only, no APM SaaS.
14. **Implementation risk?** Low technically, but it is a cross-cutting
    infrastructure change touching nearly every file in `app/`, which is a
    large diff for what is fundamentally a debugging aid, not a new
    memory-engineering capability.
15. **How would success be measured?** Harder to state concretely than for
    the other candidates — "logs are more structured now" is not a
    falsifiable claim the way "recall on category X improved from Y% to
    Z%" is, which sits uneasily with this project's own stated evaluation
    philosophy.

**Conclusion for this candidate:** real and eventually valuable, but it is
infrastructure hygiene, not a memory-engineering capability, and does not
directly advance the project's core thesis the way the brief asks Phase 7
to. Better pursued alongside Phase 8's productionization work, where
"ready for untrusted traffic" already demands it, than as a freestanding
Phase 7.

### 2.4 Entity resolution improvements

1. **What problem does it solve?** Today, ambiguity detection
   (`app/extraction/resolution.py`) is a canonical-key + embedding-
   similarity *flag*, reviewed by a human — it never auto-merges (Phase 3/4,
   deliberately). A candidate here would be: use a real LLM to *adjudicate*
   flagged pairs (is "OpenAI" the same as "Open AI" here?) rather than
   leaving every flag for manual review.
2. **Why is it needed after Phase 1–6?** No phase has measured that manual
   review of flagged pairs is a real bottleneck — this would be solving an
   unmeasured problem.
3. **Which existing limitation does it address?** None explicitly
   documented; the closest is `PHASE4_PLAN.md`'s note that the review
   workflow is "only as good as whoever drives it via HTTP" — a frontend
   gap (§10), not an entity-resolution algorithm gap.
4. **Which existing components can be reused?** `is_plausible_duplicate`,
   `EntityMergeCandidateRepository`, the existing review workflow.
5. **What new components would be required?** An LLM-adjudication step,
   which reintroduces exactly the risk ADR 0007/0010 were written to avoid:
   giving an LLM output any influence over an *identity* decision
   (merging two entities) rather than a *proposal* a deterministic engine
   evaluates. This would need very careful scoping to not violate "LLM
   proposes, application decides."
6–10. Touches Neo4j (entity merge), agent only if exposed as a tool,
   evaluation (a new adjudication-accuracy metric).
11–15. Bounded technically, but it is a narrower, riskier slice of exactly
   the same "does the agent/LLM layer make good judgment calls" question
   that candidate 2.1 already investigates more broadly (and more safely,
   since candidate 2.1 never lets model judgment influence canonical
   identity, only measures and — if warranted — nudges *retrieval*
   behavior via prompt text, not a structural decision).

**Conclusion for this candidate:** a real idea, but a narrower, higher-risk
special case of what candidate 2.1 already covers at the evaluation layer,
with no measured evidence it's currently a bottleneck. Worth revisiting
once Phase 7's expanded evaluation gives real data on how often ambiguous-
entity flags actually occur in practice.

---

## 3. Recommendation

**Phase 7 = Unified Evaluation Framework & Agent Retrieval-Reliability**
(candidate 2.1). It is the only candidate that: follows the project's own
already-stated roadmap; is directly evidenced by name in every limitation
the brief asked to be explicitly accounted for; reuses every existing
component instead of adding a new architectural surface; carries the
lowest risk (no canonical-memory writes, no new agent authority, no new
dependency); and produces a durable capability (historized, comparable
evaluation) that every *other* candidate on the list (consolidation,
observability, entity resolution, and whatever Phase 8 turns out to need)
will itself benefit from having.

---

## 4. How each named current limitation is addressed (not silently worked around)

| Limitation | Phase 7 treatment |
|---|---|
| Real Gemini document evaluation quota-limited (1/5) | A resumable, checkpointed real-LLM runner (§7.4) lets a later run continue from task 2 instead of re-spending quota on task 1's already-successful call. Does **not** add retries, does **not** change the dataset, does **not** claim completion that didn't happen. |
| Document real-LLM validation partial | Explicitly tracked as an open row in `evaluation_runs` (§6) with a `coverage_fraction` field, rather than only living in prose in `PHASE6_PLAN.md`. Phase 7 does not claim to close this gap by fiat — it makes the gap durably visible and resumable. |
| Local document storage not durable/shared across instances | Out of scope for Phase 7 (it's a Phase 6/ADR 0013 deployment trade-off, not an evaluation gap) — explicitly listed in §9 as deferred, not silently dropped. |
| Baseline B raw-chunk evaluation minimal | Extended from 2 tasks / top-1-hit to every applicable document task / `recall@k` (§7.4), still strictly evaluation-only, still no production vector index for chunks (ADR 0014 unchanged). |
| Phase 5 `ambiguous_entity` real-LLM failure | Generalized from a single anecdote into a measured category (`retrieval_necessity`, §5), with a conditional, evidence-gated, non-task-specific `SYSTEM_PROMPT` addition only if the broader category shows a systematic gap — exactly the treatment `PHASE5_PLAN.md` itself said this finding deserved ("worth watching in a future evaluation expansion"). |
| Document evaluation currently deterministic/FakeLLM for most coverage | Unchanged by design for the *mechanics* harness (this is intentional methodology, per `eval/runner.py`'s own stated scope, not a defect) — Phase 7 improves the *real-LLM* section's resumability instead of trying to make the deterministic section pretend to be something it isn't. |
| Project remains zero-cost | See §8 — nothing in this plan requires a paid service, and the checkpoint mechanism specifically reduces free-tier quota pressure rather than increasing it. |
| Existing memory/entity/retrieval architecture must remain stable | Confirmed component-by-component in §2.1 items 6–10 and §7's "unchanged" list — zero changes to `MemoryService`, `RetrievalService`, `HybridRetrievalService`, the decision engine, or any Neo4j/Cypher query. |

---

## 5. Evaluation-first design

This section is written *before* any implementation, per the brief's
explicit requirement, and follows `EVALUATION.md`'s own existing
methodology rather than inventing a new one.

**Baseline (current, measured state — not assumed):**
- Agent evaluation (`eval/agent_runner.py`, `agent_v1.jsonl`): 9 tasks,
  8 categories, one of which (`ambiguous_entity`) is a single task scoring
  0% recall under real-LLM evaluation.
- Document evaluation (`eval/document_runner.py`, `document_v1.jsonl`):
  10 tasks covering all 11 plan categories deterministically; real-LLM
  section attempted 5 tasks, 1 completed (`doc_basic_extraction`, 100%
  hit), 4 hit `HTTP 429`.
- Baseline B: 2 tasks, top-1 hit/miss only.
- No cross-harness result history exists; every number above currently
  lives only in prose inside `PHASE5_PLAN.md`/`PHASE6_PLAN.md`.

**Target behavior:**
- A `retrieval_necessity` category exists with ≥5 tasks spanning: an
  ambiguous real-world-sounding entity name (generalizing
  `ambiguous_entity`), a query that plausibly sounds like general
  knowledge but has a seeded, user-specific memory that contradicts or
  extends it, a query where the correct behavior *is* to answer from
  general knowledge (no memory exists, and none should be fabricated —
  this is the crucial negative-result control, since a category with only
  "should search" tasks could be gamed by a prompt that just always
  searches), and at least two more varied phrasings of the same underlying
  judgment call.
- Recall on this category is measured (not assumed) both before and after
  any prompt change; the existing `agent_v1.jsonl` suite's other
  categories are re-measured on the same before/after runs to confirm no
  regression (the ablation, per the brief's explicit requirement).
- Baseline B covers every document task that has an `answer_contains`
  field, using `recall@k` (k=1 and k=3) rather than only a single top-1
  hit/miss.
- A real-LLM evaluation run, once started, can be safely re-invoked later
  (a different session, a different day) and will skip tasks already
  recorded as completed rather than re-calling the API for them.

**Metrics:**
- Recall/precision per category (existing `eval/metrics.py` machinery,
  unchanged).
- `recall@1`/`recall@3` for baseline B (new, small addition to
  `eval/metrics.py` or a local computation in `document_runner.py`,
  whichever avoids duplicating logic already in the file that computes it
  for other purposes).
- Coverage fraction (`completed / attempted`) per real-LLM run, persisted
  in `evaluation_runs` (§6).
- Regression detection: a run is flagged if any category's recall drops
  by more than a fixed, documented tolerance versus the immediately
  preceding recorded run of the same harness — not versus an arbitrary
  historical best, to avoid rewarding a lucky run.

**Test dataset requirements:**
- `retrieval_necessity` tasks must include entity names not present in
  `agent_v1.jsonl` today, to avoid the exact contamination risk the
  ambiguous_entity finding itself illustrates (a name the model might
  already associate with a real-world entity).
- Every new task must include an explicit `expected_seed_indices` (or
  equivalent) so a "no memory should be cited" task and a "memory should
  be cited" task are both structurally distinguishable from a passing
  script, not just from prose in the task's `query` field.

**Regression tests (backend/tests, real code, not eval scripts):**
- `eval/report.py`'s aggregation logic (does it correctly combine N
  `ScenarioResult` lists into one summary, does it correctly compute a
  regression flag against a fixed prior-run fixture).
- The new `EvaluationRunRepository` (create/list/user-scoping-N/A since
  this is operational not user data — see §6/§8).
- The checkpoint file's resume logic (given a partial checkpoint naming
  2 of 5 tasks complete, the next invocation must run only the remaining
  3 — testable entirely with `FakeLLMClient`, no real API call needed to
  verify the *mechanism*).
- A regression test proving the conditional `SYSTEM_PROMPT` addition (if
  made) does not change behavior on any existing, previously-passing
  `agent_v1.jsonl` task (an explicit non-regression assertion, not just
  "the new category passes").

**Failure categories to track distinctly (per `EVALUATION.md`'s own taxonomy):**
retrieval-necessity misses (agent should have searched, didn't — the
`ambiguous_entity` shape), false-positive searches (agent searches when it
shouldn't need to — a new failure mode this category's negative-control
tasks would surface for the first time), citation errors (unchanged from
existing coverage), quota/availability failures (real-LLM section,
reported as `HTTP 429`/similar, never conflated with a real miss).

**Ablation opportunities:**
- System prompt with vs. without the new instruction (if one is added),
  measured on both the new category and the full existing suite.
- Baseline B's `recall@1` vs. `recall@3`, to characterize how much of
  the gap between naive chunk retrieval and canonical-memory retrieval is
  about ranking depth versus genuine synthesis (candidate memories may
  aggregate two chunks; a raw chunk can't).

**No benchmark results are asserted in this document.** Every number in
this section is either an already-measured baseline (cited from the real
Phase 5/6 reports) or explicitly marked as a target to be measured after
implementation.

---

## 6. Architecture

### 6.1 Components

- **`eval/report.py`** (new): orchestrates the six existing harnesses (by
  importing and calling their existing `main()`/scenario functions, or by
  reading their result objects when run as a library — whichever avoids
  duplicating any harness's own logic), writes one consolidated markdown
  report, and persists a summary row to `evaluation_runs`.
- **`eval/checkpoint.py`** (new, small): a plain JSON file
  (`eval/.checkpoints/<harness>_<dataset_version>.json`, gitignored, mirrors
  how `backend/data/` is already gitignored) recording which task ids
  completed successfully in a real-LLM run; read at the start of a run,
  written after each task completes. No secrets are ever written to it —
  only task ids and pass/fail, matching the project's existing "never log
  sensitive content" discipline (`CLAUDE.md` §32).
- **`app/repositories/evaluation_repository.py`** (new): a
  `UserScopedRepository`-*adjacent* but explicitly **not** user-scoped
  repository (mirrors `ExtractionQueue`'s documented, narrow exemption —
  evaluation runs are operational data, not any one user's data) for
  `evaluation_runs` CRUD.
- **`app/models/evaluation.py`** (new): the Pydantic domain model for one
  evaluation run.
- **Extended, not replaced:** `eval/metrics.py` (add `recall_at_k`-style
  helper reuse already exists — `eval/metrics.py` already has
  `recall_at_k`/`precision_at_k` used by `agent_runner.py`; baseline B
  reuses these directly rather than inventing new math), `eval/
  agent_runner.py` (new category added to its dataset, checkpoint
  integration added to its real-LLM section), `eval/document_runner.py`
  (baseline B depth, checkpoint integration), `app/agent/prompts.py`
  (conditional one-line addition to `SYSTEM_PROMPT`, only if warranted).

### 6.2 Data flow

```
eval/report.py
  |
  +--> eval/runner.py (unchanged) ------------------\
  +--> eval/retrieval_runner.py (unchanged) ----------\
  +--> eval/duplicate_threshold_calibration.py (unchanged) --\
  +--> eval/graph_scale_benchmark.py (unchanged) --------------> ScenarioResult /
  +--> eval/agent_runner.py (+ retrieval_necessity     --------/  EvaluationReport
  |      category, + checkpoint-aware real-LLM section)------/   objects (existing
  +--> eval/document_runner.py (+ deeper baseline B, --------/    shapes, unchanged)
         + checkpoint-aware real-LLM section)
  |
  v
build_report() (existing, unchanged) --> consolidated summary
  |
  v
EvaluationRunRepository.create() --> PostgreSQL: evaluation_runs (new)
  |
  v
printed report (existing behavior, unchanged) + optional prior-run diff
```

### 6.3 What changes, per layer

- **PostgreSQL:** one new table (`evaluation_runs`, §7). No existing table
  changes.
- **Neo4j:** unchanged. No new node/relationship types, no new Cypher.
- **API:** one small, optional addition — `GET /internal/evaluation/runs`
  (gated by the existing `InternalTokenDep` pattern, mirroring `POST
  /internal/extraction/sweep`) to list recent runs. Explicitly optional and
  deferrable (§9) — it exists only so a future Phase 9 dashboard has
  something to read; it is not required for Phase 7's own evaluation work,
  which runs entirely via CLI like every prior phase's evaluation.
- **Agent:** conditional, narrow `SYSTEM_PROMPT` addition only if the
  `retrieval_necessity` category shows a systematic gap (§5); no tool
  changes, no loop-control changes, no new dependency.
- **Retrieval:** unchanged.
- **Document ingestion:** unchanged.
- **Evaluation:** this phase's entire subject — see §5/§6.1.
- **Frontend:** unchanged (none exists; see §10).
- **Deployment:** unchanged; the checkpoint file is a local artifact like
  `backend/data/`, gitignored, not part of any deployment topology change.

### 6.4 Explicitly unchanged (confirmed by direct inspection, not assumed)

`MemoryService`, `RetrievalService`, `HybridRetrievalService`,
`ExtractionRunner` and the full decision engine
(`app/extraction/decisions.py`), `ProvenanceService`, `AgentService`'s loop
control, every existing tool in `app/agent/tools.py`, the document
ingestion pipeline (`app/documents/*`), `LocalDocumentStorage`, every
existing PostgreSQL table, every existing Neo4j constraint/index, the
`X-User-Id` identity placeholder (ADR 0005 — still not real auth; Phase 7
does not touch authentication).

---

## 7. Data model

### 7.1 `evaluation_runs` (new PostgreSQL table)

**Purpose:** durable, comparable history of evaluation-harness invocations
— the concrete need this fills is stated in §2.1/§6: without it, every
evaluation result lives only in a human-transcribed markdown table with no
way to detect a regression automatically.

**Fields:**

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `harness` | varchar | e.g. `"agent"`, `"document"`, `"extraction"`, `"retrieval"` — which of the six scripts produced this row |
| `dataset_version` | varchar | e.g. `"agent_v1"`, `"document_v1"` — ties a run to the exact dataset file it used |
| `mode` | varchar | `"deterministic"` or `"real_llm"` — never conflated (mirrors the project's own existing, repeated discipline of keeping these separate in every report) |
| `llm_provider` | varchar, nullable | recorded only for `real_llm` runs, e.g. `"gemini"` — never the API key |
| `started_at` / `completed_at` | timestamptz | |
| `total_tasks` | integer | |
| `completed_tasks` | integer | for a quota-limited real-LLM run, may be less than `total_tasks` |
| `mean_recall` / `mean_precision` | float, nullable | aggregate metrics, matching what every existing report already prints |
| `category_breakdown` | jsonb | `{category: {recall, precision, n}}` — the per-category table every existing markdown report already includes, now structured |
| `git_commit` | varchar, nullable | the commit the run was produced against, for correlating a result with a code change |
| `created_at` | timestamptz | row insert time |

**Ownership:** not user-scoped (operational/developer data, like
`extraction_queue`'s cross-user exemption) — explicitly documented as such,
mirroring the existing `EXEMPT_FILES` pattern in `tests/unit/
test_sql_safety.py` if the repository needs the same static-audit
exemption `ExtractionQueue`/`IngestionQueue` already have.

**Lifecycle:** append-only, like every other table in this project
(`ADR 0002`'s discipline extended by convention, not by a new hard
constraint) — a run row is never edited after `completed_at` is set.

**Indexes/constraints:** index on `(harness, dataset_version, mode,
created_at)` for the "give me the most recent prior run of this exact
configuration" regression-comparison query; no uniqueness constraint
needed (re-running the same harness/dataset/mode is the normal case, not
an error).

**User isolation:** N/A — contains no user memory content, only aggregate
counts/scores and task ids. This is a deliberate design constraint (see
§8): raw memory content or document content must never be written into
`category_breakdown` or any other field here.

**Temporal behavior:** simple insert-only history; no supersession model
needed (unlike `Memory`, a run doesn't get "corrected," a new run is just
a new row).

**Provenance:** `git_commit` plays the provenance role — which code
produced this number.

**Idempotency:** not required at the row level (each invocation is a new,
distinct event); the *checkpoint file* (§6.1) is where idempotency for a
real-LLM run's individual task calls lives, kept deliberately separate
from this table so a checkpoint (which must survive an interrupted run) is
not coupled to a completed run's summary row (which is only written once
the run finishes).

### 7.2 Dataset file additions

`eval/datasets/agent_v1.jsonl`: append ≥5 new tasks under a new
`category: "retrieval_necessity"` (or extend to `agent_v2.jsonl` if adding
in-place risks disturbing the exact task set Phase 5's own report already
references by name — decided during implementation based on which reads
more clearly, not decided here).

`eval/datasets/document_v1.jsonl`: no new tasks required; existing tasks
already have `answer_contains` where meaningful — baseline B's depth
increase is a harness-code change, not a dataset change.

No new Neo4j labels, relationship types, or PostgreSQL tables beyond
`evaluation_runs`.

---

## 8. Security

- **Cross-user isolation:** `evaluation_runs` holds no per-user data by
  design (§7.1) — there is nothing to isolate, which is itself the safest
  posture rather than adding user-scoping machinery to data that isn't a
  user's.
- **Prompt injection:** unaffected — no change to how document or message
  content reaches an LLM prompt; the new `retrieval_necessity` tasks are
  themselves eval fixtures, not a new injection surface.
- **Untrusted document content:** unaffected — no ingestion-pipeline
  changes.
- **Agent write authority:** unaffected — `propose_memory_update` and
  `AGENT_WRITE_ENABLED` are untouched; the only agent change under
  consideration is read-path *judgment* (should I search?), never a new
  write capability.
- **Provenance integrity:** unaffected.
- **Secret handling:** the checkpoint file and `evaluation_runs` table are
  both explicitly specified to hold task ids/scores/provider *names* only
  — never an API key, never raw memory or document content. This is
  called out here precisely because it is the one place this phase adds a
  new place secrets or user content *could* accidentally leak if the
  implementation were careless, and the plan forecloses that before
  implementation begins.
- **API abuse:** the one new endpoint (`GET /internal/evaluation/runs`,
  optional) is gated by the existing `InternalTokenDep` constant-time-
  comparison pattern — no new auth mechanism invented.
- **Data deletion/forgetting:** out of scope (see §2.2/§9) — not touched
  by this phase.

---

## 9. UX / Frontend

**None.** No frontend work is included in Phase 7. This follows directly
from §0.1's finding: `frontend/` has zero application code, the project's
own roadmap places UI work at Phase 9 after authentication (Phase 8), and
nothing in the recommended capability requires a UI — every existing
evaluation harness (and the new report aggregator) is a CLI tool, matching
every prior phase's own evaluation tooling. If a future Phase 9 dashboard
wants to visualize `evaluation_runs` over time, the optional read-only
endpoint in §6.3 gives it something to call — but building that dashboard
now, with no auth and no other UI to put it in, would be exactly the kind
of premature, disconnected feature work the brief warns against.

---

## 10. Proposed ADR candidates (not created yet)

1. **"Evaluation run history lives in PostgreSQL, never Neo4j."**
   *Decision:* `evaluation_runs` is application/operational state, under
   the same boundary ADR 0006 already drew for conversations/extraction
   state — Neo4j remains exclusively canonical memory, never a place to
   record how well the system was measured to perform.
   *Why it matters:* without stating this explicitly, a future contributor
   might reach for Neo4j (since "everything about memory quality feels
   graph-adjacent") and blur the exact boundary ADR 0006 was written to
   keep sharp.

2. **"Agent system-prompt changes require dataset-level evidence, not
   single-task patches."**
   *Decision:* codifies a discipline the project has followed informally
   since Phase 5 (`ambiguous_entity` was explicitly *not* patched with
   task-specific language) — a `SYSTEM_PROMPT` change is only accepted
   when a measured category (≥5 tasks, including negative controls) shows
   a systematic gap, and the change must be shown, by re-running the full
   existing suite, not to regress any other category.
   *Why it matters:* without a named rule, a future contributor could
   "fix" one failing eval task with prompt text that quietly overfits to
   that exact task's wording — exactly the failure mode `app/agent/
   prompts.py`'s own existing comment already warns against informally.

3. **"Real-LLM evaluation runs are checkpointed and resumable, never
   blindly retried."**
   *Decision:* a rate-limited real-LLM run persists per-task completion
   state locally and resumes from it, rather than either (a) looping with
   its own retry/backoff beyond what `LLMClient` already bounds, or (b)
   silently re-running already-succeeded tasks and wasting quota.
   *Why it matters:* this is the direct, generalized fix for a problem
   that has now independently occurred in both Phase 5 (9-task run split
   across two days) and Phase 6 (1/5 completed) — worth a named,
   intentional decision rather than an ad hoc workaround repeated a third
   time in a future phase.

---

## 11. Implementation sequence

Each stage is independently testable and independently revertable; no
stage requires a later stage to already exist to be verified.

**Stage 1 — `evaluation_runs` table + repository**
- *Objective:* durable storage for run summaries, with nothing yet writing
  to it.
- *Files:* new Alembic migration, `app/db/models.py` (new `EvaluationRun`
  ORM class), `app/models/evaluation.py` (domain model), `app/
  repositories/evaluation_repository.py`.
- *Tests:* repository unit/integration tests (create, list, order by
  `created_at`); `tests/unit/test_sql_safety.py`'s exemption list updated
  only if the repository needs the cross-user exemption (likely yes, per
  §7.1) — mirrors the exact update made for `ingestion_queue.py` in
  Phase 6.
- *Verification:* migration applies cleanly to a fresh test database and
  to the existing populated one; `alembic check` clean.
- *Rollback/risk:* trivial — an unused, empty table; `alembic downgrade`
  drops it cleanly.

**Stage 2 — `eval/report.py` aggregator (read existing results only)**
- *Objective:* one command produces one consolidated report from the six
  existing harnesses' own result objects, and writes a summary row per
  harness invocation to `evaluation_runs`.
- *Files:* new `eval/report.py`; no changes yet to any existing harness.
- *Tests:* aggregation logic tested against fixture `ScenarioResult`/
  `EvaluationReport` objects (no real database/LLM call needed for this
  part).
- *Verification:* run against the real (test) databases with
  `LLM_PROVIDER=fake`; confirm a row appears in `evaluation_runs` per
  harness and the printed report matches each harness's own existing
  stdout output.
- *Rollback/risk:* low — purely additive, orchestration-only file.

**Stage 3 — checkpoint mechanism for real-LLM runs**
- *Objective:* a rate-limited real-LLM run can be safely re-invoked and
  will skip already-completed tasks.
- *Files:* new `eval/checkpoint.py`; small integration into `eval/
  agent_runner.py`'s and `eval/document_runner.py`'s real-LLM sections
  only (their deterministic sections are untouched).
- *Tests:* checkpoint read/write/resume logic, fully testable with
  `FakeLLMClient` (no real API call needed to prove the *mechanism*
  works — simulate "task 1 already in checkpoint" and assert task 1 is
  skipped).
- *Verification:* a deliberately-interrupted fake run (kill after task 2
  of 5) resumes correctly on the next invocation, processing only tasks
  3–5.
- *Rollback/risk:* low — additive only; deleting the checkpoint file
  reverts to today's always-start-from-task-1 behavior with zero code
  impact.

**Stage 4 — `retrieval_necessity` evaluation category**
- *Objective:* generalize the `ambiguous_entity` finding into a measured
  category.
- *Files:* `eval/datasets/agent_v1.jsonl` (or `agent_v2.jsonl`), no
  `eval/agent_runner.py` logic changes beyond wiring the new category
  through the existing baseline machinery (already generic across
  categories).
- *Tests:* the deterministic (`E_scripted_mechanics`) path for every new
  task, proving the harness mechanics work before spending any real-LLM
  quota on them.
- *Verification:* run the full existing `agent_v1.jsonl` suite
  deterministically — must still be 100% (or exactly whatever it was
  before, unchanged) to prove the addition didn't disturb anything.
- *Rollback/risk:* low — additive dataset tasks only.

**Stage 5 — baseline B depth**
- *Objective:* extend `eval/document_runner.py`'s baseline B from 2 tasks/
  top-1 to every applicable task / `recall@k`.
- *Files:* `eval/document_runner.py` only.
- *Tests:* baseline B's ranking logic against fixture chunks (already
  partially covered; extend for `k>1`).
- *Verification:* re-run `eval/document_runner.py`'s deterministic section
  — the 10 existing `ScenarioResult`s must remain unchanged (baseline B is
  reported separately, never folded into pass/fail, per ADR 0014's
  existing discipline — confirmed still true after this change).
- *Rollback/risk:* low — isolated to one already-isolated function.

**Stage 6 — measure, then (conditionally) the prompt change**
- *Objective:* run Stage 4's category with the real LLM (via Stage 3's
  checkpointing, respecting quota); if it shows a systematic gap, make
  the one general `SYSTEM_PROMPT` addition and re-measure.
- *Files:* possibly `app/agent/prompts.py` (one instruction, conditional
  on Stage 6's own measured result — not decided in advance by this plan).
- *Tests:* the explicit non-regression test named in §5 (full existing
  `agent_v1.jsonl` suite unaffected).
- *Verification:* before/after recall on `retrieval_necessity`, reported
  honestly whichever way it comes out (this document does not pre-commit
  to "the prompt change will work" — per `EVALUATION.md`'s own philosophy,
  a negative result here is a valid, reportable Phase 7 outcome).
- *Rollback/risk:* the only stage with agent-behavior risk; mitigated by
  the non-regression gate and by the change being additive text, trivially
  revertable.

**Stage 7 — final Phase 7 report**
- *Objective:* one consolidated report (per §6.1) covering all six
  harnesses' current state, the new category's measured result, baseline
  B's deeper coverage, and an honest statement of what remains partial
  (real-LLM document coverage still quota-bound, per §4).
- *Files:* none (a report, not code).
- *Verification:* full existing test suite (736+ new tests from stages
  1–6) still passing; `ruff check` clean.

---

## 12. Scope boundaries

### In scope for Phase 7
- `evaluation_runs` table + repository (Stage 1)
- Consolidated evaluation report aggregator (Stage 2)
- Checkpointed/resumable real-LLM evaluation runs (Stage 3)
- `retrieval_necessity` evaluation category, generalizing `ambiguous_entity`
  (Stage 4)
- Deeper baseline B (`recall@k`, full task coverage) (Stage 5)
- One conditional, evidence-gated, general-purpose `SYSTEM_PROMPT`
  addition, with a full non-regression ablation (Stage 6)
- One optional, internal-token-gated read endpoint for run history

### Out of scope for Phase 7 (deferred, not forgotten)
- Memory consolidation / decay / archival sweep (§2.2) — real, but
  premature; revisit once Phase 7's own data on retrieval/citation
  patterns gives evidence of actual need.
- Structured observability/tracing (§2.3) — pair with Phase 8's
  productionization work instead.
- LLM-assisted entity-resolution adjudication (§2.4) — revisit with real
  data on how often ambiguous-entity flags occur, which Phase 7's own
  review-workflow usage (unchanged, still HTTP-only) does not yet provide
  and this phase does not add either.
- Any frontend/UI work (§9/§10) — Phase 9, after Phase 8 auth, per the
  existing roadmap.
- Authentication/multi-user (Phase 8, per the existing roadmap).
- Deployment architecture changes (Phase 9, per the existing roadmap).
- `LocalDocumentStorage`'s durability/shared-instance limitation (ADR
  0013) — a Phase 6 storage-architecture trade-off, not an evaluation gap;
  revisit only if a genuinely free, zero-setup, no-billing-relationship
  object storage option is identified (per ADR 0013's own stated
  condition).
- Any change to the extraction decision engine, retrieval algorithm,
  document chunking, or Neo4j schema.
- Rate limiting / API abuse protection beyond the existing internal-token
  pattern (belongs with Phase 8 auth work).

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Scope creep into "rewrite the whole eval system" | §12's explicit boundary: extend, never replace, the six existing harnesses; `eval/report.py` orchestrates, it does not reimplement. |
| Conditional prompt change regresses another category | Mandatory full-suite re-run before acceptance (Stage 6); negative result is an acceptable, reportable Phase 7 outcome, not a blocker to completing the phase. |
| `evaluation_runs` becomes an accidental second source of truth for memory quality claims | Explicitly scoped to aggregate counts/scores only (§7.1/§8) — never raw memory/document content; `git_commit` field ties every row back to the actual code, so a claim is always traceable to a real run, not asserted from the table alone. |
| Checkpoint file drifts out of sync with the dataset (stale completions recorded against an old dataset version) | Checkpoint filename includes `dataset_version` (§6.1) — a dataset change produces a fresh checkpoint automatically, never a false "already complete." |
| Real-LLM quota still insufficient even with checkpointing | Explicitly acceptable and pre-declared in §4/§5 — the plan does not promise full real-LLM coverage, only better use of whatever quota is available, reported honestly either way. |
| New table adds migration/ops surface for no measured benefit | §2.1 item 15 and §5 both state concrete, falsifiable success criteria in advance, so the table's value is checked, not assumed. |

---

## 14. Definition of done

Phase 7 is complete when:

1. `eval/report.py` produces one consolidated report covering all six
   existing evaluation harnesses in a single invocation.
2. Every harness invocation's summary is persisted to `evaluation_runs`
   and a regression check against the immediately preceding same-
   configuration run is computed and printed.
3. A rate-limited real-LLM evaluation run (agent or document) can be
   re-invoked after hitting `HTTP 429` and correctly skips already-
   completed tasks, verified by a deterministic test simulating an
   interrupted run.
4. The `retrieval_necessity` category exists with ≥5 tasks including at
   least one negative control, and its deterministic-mechanics recall is
   measured and reported.
5. A real-LLM run of `retrieval_necessity` has been attempted (not
   necessarily fully completed, per the honest-quota-reporting norm) and
   its result — whichever way it comes out — is reported without
   fabrication.
6. If a `SYSTEM_PROMPT` change was made, the full pre-existing
   `agent_v1.jsonl` suite is shown, by an actual re-run, not to have
   regressed.
7. Baseline B covers every document task with an `answer_contains` field,
   reporting `recall@1` and `recall@3`.
8. All pre-existing tests (736 as of the Phase 6 checkpoint) plus new
   tests for Stages 1–5 pass.
9. Zero-cost verified exactly as in every prior phase's report: no new
   paid service, no new paid dependency, `evaluation_runs` on the existing
   local PostgreSQL, checkpoint files on local disk.
10. A final Phase 7 report states plainly what was measured, what
    improved, what did not, and what remains open — including, if still
    true at that point, continued partial real-LLM document coverage —
    exactly the honesty standard every prior phase's report in this
    project has held itself to.

---

## Implementation Status (Phase 7 complete)

**Note:** this section was appended after implementation, mirroring how
`PHASE5_PLAN.md`/`PHASE6_PLAN.md` record their own measured results in
place rather than in a separate report file. Everything above this line is
the plan as approved; everything below is what was actually built and
measured, updated after the fact.

### What was built

* **`evaluation_runs`/`evaluation_task_results`** (PostgreSQL, migration
  `ec358e32d566`) — exactly as designed in §7.1, no changes. ADR 0015.
* **`app/models/evaluation.py`**, **`app/repositories/evaluation_repository.py`**
  — domain models and persistence, not user-scoped (no `user_id` column
  exists on either table).
* **`eval/persistence.py`** — `EvaluationRunTracker`/`TaskOutcome`, the
  checkpoint/resume layer. ADR 0017.
* **`eval/report.py`** — read-side aggregation/regression-comparison CLI
  (`--run-id`, `--harness ... --latest`).
* **`retrieval_necessity`** — 6 tasks appended to `eval/datasets/agent_v1.jsonl`
  (not a separate file — decided during implementation, since it reuses
  every existing baseline/loading function with zero code changes),
  covering all six required cases from the brief (required, not-required,
  ambiguous-entity, absent, user-specific, general-knowledge), with entity
  names deliberately distinct from `ambiguous_entity`'s own fixture.
* **Baseline B strengthened** — `recall@1`/`recall@3` (was: single top-1
  hit/miss), extended from 2 tasks to 5 (every document task with a
  well-defined `answer_contains`), with two genuinely new, honestly
  documented structural findings (below).
* **Both real-LLM harness sections** (`agent_runner.py`, `document_runner.py`)
  now go through `EvaluationRunTracker`: resumable, rate-limit-aware, stop
  cleanly on `HTTP 429` rather than continuing to call the API.
* **`app/agent/prompts.py`: unchanged.** See "Prompt decision" below.
* Three ADRs: 0015 (evaluation history in PostgreSQL), 0016 (prompt changes
  require dataset-level evidence), 0017 (real-LLM runs are checkpointed).
* `docs/EVALUATION.md`'s stale implementation-status header updated;
  root `README.md`'s phase-status table and repository diagram corrected
  (Phase 6 was already complete but still showed "Not started"; ADR count
  was still "0001-0008").

### Test results

**769 tests passing** (549 unit / 220 integration), up from 748 at the
Phase 6 checkpoint. New this phase: 12 persistence tests (run creation,
resume, rate-limit preservation, duplicate-completion prevention,
deterministic ordering, N/A-not-zero metric semantics, regression-comparison
lookup — `tests/integration/test_evaluation_persistence.py`), 8 security
tests (no Neo4j import path from the evaluation layer, `extra="forbid"` on
both domain models, no raw-content-shaped fields —
`tests/unit/test_evaluation_security.py`), 13 dataset-structure tests
(JSONL validity, the six required `retrieval_necessity` cases, all 11
document categories present — `tests/unit/test_eval_dataset_structure.py`).
All pre-existing Phase 2-6 tests pass unchanged. `ruff check app tests eval`
clean.

### Deterministic evaluation results (real, measured, FakeLLM mechanics)

Agent harness, 15 tasks (9 original + 6 `retrieval_necessity`),
`E_scripted_mechanics`: **100% recall, 100% precision** on every task,
including all 6 new ones. This proves the mechanics (retrieval, citation
validation, tool dispatch) work correctly for the new category — it is
**not** evidence about real model judgment (see "Real-LLM evaluation"
below; these two are never conflated, per this project's own established
discipline).

Document harness, 10 tasks (all 11 planned categories), deterministic
section: **100% decision accuracy**, all 10 tasks passing, unchanged from
the Phase 6 checkpoint.

**Baseline B** (evaluation-only, 5 tasks): mean recall@1 = 90.0%, mean
recall@3 = 100.0%. Per-task:

| Task | recall@1 | recall@3 | Finding |
|---|---|---|---|
| `doc_basic_extraction` | 100% | 100% | — |
| `doc_cross_source_duplicate` | 100% | 100% | — |
| `doc_multi_document_reasoning` | 50% | 100% | top-1 cannot cover facts split across two documents; top-3 recovers them by ranking-depth accident, not synthesis |
| `doc_document_and_conversation_combined` | 100%* | 100%* | *structural ceiling: the conversation-derived half of the answer exists in no document chunk at all — baseline B could never find it regardless of ranking depth; the 100% here reflects only the document-derived half of `answer_contains` |
| `doc_temporal_facts` | 100% | 100% | structural: baseline B has no as-of mechanism at all — same top chunk regardless of requested date |

These are the honest, measured "what does canonical memory extraction add
over naive chunk retrieval" findings the plan's Baseline B section asked
for: mostly parity at small scale, with two genuine, structural (not just
lower-accuracy) gaps — multi-document synthesis and cross-source
(document+conversation) combination — that no amount of chunk-ranking
depth can close.

### Real-LLM evaluation

Provider/model: `gemini` / `gemini-2.5-flash` (the same key used in every
prior phase's real-LLM validation).

**Agent harness (`retrieval_necessity`, isolated via `EVAL_ONLY_CATEGORY`):**
3 separate attempts this session, all blocked identically: `retrieval_necessity_required`
attempted, `HTTP 429` on the actual call (after the client's own bounded
retry already failed 2-3 times per attempt at the SDK level). **0 of 6
tasks completed.** The run (`evaluation_runs` row, checkpointed) remains
`in_progress`/resumable; a session with working quota can continue it
without re-spending anything already attempted.

**Document harness:** 1 attempt, same result — `doc_basic_extraction`
attempted, `HTTP 429`. **0 of 5 tasks completed.**

This is reported exactly as it happened: **REAL LLM RATE LIMITED** for
both harnesses, not **REAL LLM COMPLETED**, not **REAL LLM FAILED** (this
is not a code/schema error — the same class of `LLMResponseInvalidError`
compatibility bugs Phase 5 found and fixed are not implicated; every
failure this session was `LLMUnavailableError` with `HTTP 429`), and not
**REAL LLM NOT RUN** (three genuine attempts were made). No retry loop was
added beyond `LLMClient`'s existing bounded backoff; no result was
fabricated or backfilled with a FakeLLM substitute.

### Retrieval-necessity results

Deterministic mechanics: 100% recall (6/6 tasks, scripted). Real-LLM: no
data obtained this session (rate-limited, above) — **not** reported as 0%
or as evidence of any kind about real model behavior.

### Prompt decision

**Evaluation evidence did not justify a system-prompt modification.**

Not because the broader `retrieval_necessity` category was run and found
fine — it could not be run against the real LLM this session at all. Per
ADR 0016 (written specifically to prevent exactly this ambiguity from being
glossed over): a single pre-existing anecdote (Phase 5's `ambiguous_entity`)
does not meet the bar for a general, evidence-based prompt change on its
own, and "we could not obtain broader evidence" is not treated as
equivalent to "the broader evidence showed no problem." `app/agent/prompts.py`
is unchanged. The now-resumable run (§ "Real-LLM evaluation" above) is the
correct mechanism for gathering that evidence whenever quota allows —
recommended as the concrete next step, not re-attempted further this
session per the "do not repeatedly hammer the API" instruction.

### Performance (actual measured values only)

* Deterministic agent harness (15 tasks, all baselines A-E-scripted): a few
  seconds wall-clock (FakeLLM/FakeEmbedding, no network).
* Deterministic document harness (10 tasks): ~3s wall-clock (consistent
  with the Phase 6 checkpoint's own measurement).
* Real-LLM attempts: each blocked call's *own* latency (including the
  client's internal bounded retry before surfacing `HTTP 429`) was
  9.1-11.0 seconds per attempt across the three agent-harness attempts and
  the one document-harness attempt — no successful real-LLM call completed
  this session, so no genuine per-task latency/token numbers are available
  to report (explicit N/A, not fabricated).

### Security verification

* `tests/unit/test_evaluation_security.py` proves, statically, that
  `eval/persistence.py`, `eval/report.py`, and
  `app/repositories/evaluation_repository.py` have no import path to
  Neo4j — evaluation cannot mutate canonical memory, by construction.
* `EvaluationRun`/`EvaluationTaskResult` are `extra="forbid"` and declare
  no raw-content-shaped field.
* Cross-run isolation verified in
  `tests/integration/test_evaluation_persistence.py` (distinct `harness`
  keys never share rows; a resumed run only ever sees its own prior task
  results).
* Every existing Phase 1-6 isolation/prompt-injection test passes
  unchanged (769/769) — nothing in the agent's tool boundaries, citation
  validation, or the document-extraction prompt's injection defense was
  touched.
* A pre-existing (not Phase 7-introduced) hygiene gap was found and
  reported, not silently fixed: `eval/agent_runner.py` has never cleaned
  up its synthetic `eval-agent-*` seed users (unlike `retrieval_runner.py`/
  `document_runner.py`, which both do) — 57 leftover rows from this
  session's and prior sessions' runs were found in the test database and
  removed as routine test-data hygiene. Documented here as a recommended
  follow-up per the "no unrelated refactors — document, don't fix" instruction,
  not fixed as part of Phase 7.

### Known limitations

* Real-LLM coverage for both the new `retrieval_necessity` category and
  the Phase 6 document tasks remains at 0% and ~20% (1/5) respectively as
  of this session — both genuinely quota-blocked, both resumable.
* Baseline B's two structural findings (multi-document synthesis,
  cross-source combination) are inherent to the "no synthesis, no
  cross-source access" design of a naive raw-chunk baseline, not bugs to
  fix — they are the finding.
* `eval/agent_runner.py`'s pre-existing lack of synthetic-user cleanup
  (above) is unresolved; a future session should add it, mirroring
  `document_runner.py`'s own cleanup block.
* The optional `GET /internal/evaluation/runs` endpoint described in §6.3
  was **not implemented** — everything in this phase was exercised via the
  CLI (`eval/report.py`), matching every prior phase's own evaluation
  tooling; the endpoint remains a documented, deferrable nice-to-have for
  a future dashboard, not built now to avoid unrequested API surface.

### Zero-cost verification

No new dependency (both new modules use only the stdlib and libraries
already in `pyproject.toml`). No paid service, no billing-required
infrastructure, no cloud storage requirement. Local PostgreSQL and local
Neo4j both confirmed working throughout (769 tests passing against both).
`FakeLLMClient`/`FakeEmbeddingClient` evaluation runs require no internet
access and were run offline-equivalent. Real Gemini access remained
strictly optional and free-tier only — no billing was enabled, no upgrade
was made, and the three rate-limited attempts were reported honestly
rather than worked around with a paid tier.

---

*Everything above the "Implementation Status" heading is the plan as
originally approved, unmodified. Everything under that heading describes
what was actually implemented and measured, appended after the fact —
Phase 7 is complete as of this section; Phase 8 has not been started.*
