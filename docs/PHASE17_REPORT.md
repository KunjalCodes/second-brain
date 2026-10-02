# Phase 17 — Full system audit, end-to-end validation and product understanding

Scope: understand, audit, integrate, validate and simplify Phases 1–16. No new feature, no
architecture redesign, no cross-space search, no deployment. Labels: **[V]** verified (test or run),
**[M]** measured, **[I]** inspected in code only, **[A]** assumption, **[L]** limitation. There is
deliberately no overall score.

Companion documents: `SYSTEM_GUIDE.md`, `SYSTEM_ARCHITECTURE.md`, `PHASE17_SYSTEM_MAP.md`,
`RETRIEVAL_ARCHITECTURE.md`, `PHASE17_EVALUATION_MAP.md`.

## 1. System overview

A graph-based persistent memory engine (FastAPI + PostgreSQL/pgvector + Neo4j) with a Next.js
explorer, an internal Memory Agent, and a project-memory layer for external coding agents (REST,
a Git collector and a stdio MCP adapter). One engine serves two memory-space kinds: personal and
project; a project is a space whose id is its own `user_id`. [V]

## 2. Architecture map

See `SYSTEM_ARCHITECTURE.md` (10 diagrams) and `PHASE17_SYSTEM_MAP.md`. 88 route/method pairs in 14
groups; every one has a contract test mapped in `tests/unit/test_route_contract_inventory.py` [V].

## 3. Data flow map

Five flows traced step by step against the code, then exercised over the live stack:
A conversation, B document, C project/coding-agent, D Git, E graph explorer.
Each write is ordered so a crash leaves recoverable state; the crash windows found are in §17.
Flow C/D were additionally driven by the 23-step scenario (§27) [V].

## 4. Memory model

Memory = statement + type + entities/relationships + source + confidence + created/updated +
`valid_from`/`valid_until` + status (active, historical, superseded, invalid, conflicting, archived,
merged). Confidence is metadata and never ranks alone [I]. Types episodic/semantic/procedural/working
are not strictly separated (accepted by the project brief). Projected structural memories skip
embeddings by design [V].
**[L]** Concepts are duplicated across schemas (three representations of decision supersession); not
unified in this phase.

## 5. Graph model

Core: Memory/Entity/Source with MENTIONS, DERIVED_FROM, RELATES_TO, SUPERSEDES, CONFLICTS_WITH,
DUPLICATE_OF, MERGED_INTO. Project graph: 14 additional entity types, multi-valued project
predicates, exact-identity types (never fuzzy-merged). Document-derived duplicates are not
auto-merged; ambiguity goes to review [V]. Every Cypher statement is user-scoped [I, audited
across `app/graph`] and covered by isolation tests [V].

## 6. Project model

`projects` row with `space_id` (`proj_<hex>`, also a `users.id`), identity (remote URL or root
commits), tokens, sessions, events, commits. Per-project isolation: tests cover project A vs B for the
same user and user A vs B across REST, MCP adapter, PostgreSQL, Neo4j, retrieval, graph,
provenance, events and resume [V]; the live scenario's stranger account saw 404s and an empty
personal search (step 23) [V].

## 7. Authentication model (credential matrix)

| Credential | Scope | Lifetime | Revocation | Valid on | Browser / non-browser |
|---|---|---|---|---|---|
| Account session | the account | server-side session with an expiry | logout, logout-all, password change | all account routes; owned project routes (+ `X-Memory-Space`) | cookie (browser); bearer only if `token_in_body` |
| Project token `sbp_` | one project, read/ingest/propose | expiring (default set by config, `ttl_days` lower allowed) | `POST …/tokens/{id}/revoke` | `/projects/{id}/…`, `/projects/current/…` only | non-browser |
| `X-User-Id` | any user | n/a | n/a | dev_header mode only | tests/dev |

Live probes against the running stack [V] (24 probes): session bearer → 200 on its own routes;
project read token → resume 200, `/auth/me` 401, `/memories/search` 401, ingest POST 403, mint
token 403, project list 401; no credential → 401 (health and OpenAPI public by design); forged
`sbp_` 401; token in cookie slot 401; `X-User-Id` alone in session mode 401; revoked token → 401;
CORS preflight allows only the configured origin (foreign origin → no allow-origin).
Expired, wrong-project and cross-user cases are covered by `tests/integration/test_projects.py`
[V]. **Inconclusive, not claimed:** the cookie/CSRF probes returned 401 because the HTTP client did
not keep a `Secure` cookie over plain http; CSRF behaviour is covered by the integration suite and
Playwright (`isolation`, `auth` specs) [V], not by this live probe.

## 8. Retrieval

See `RETRIEVAL_ARCHITECTURE.md`. Hybrid (vector + graph + keyword + deterministic rerank) for
personal memory; structural SQL/graph retrieval for projects. Temporal scopes
CURRENT/HISTORICAL/AS_OF/UNSPECIFIED. Retrieved text is labelled data. [V]
**[L]** hybrid graph channel largely inert in project spaces; no cross-space search; three
ranking/term implementations; no GIN/trigram indexes for project scans.

## 9. Agent architecture

Internal Memory Agent: bounded ReAct loop over memory tools, read-only unless
`AGENT_WRITE_ENABLED`. **Defect found and fixed:** with writes enabled, `propose_memory_update`
opened its own DB session and blocked on the conversation row lock held by the route's uncommitted
transaction — a 30 s stall and a lost write. The route now commits the user's message before the loop
(only when writes are enabled). Regression: `test_agent_turn_can_write_a_memory_through_the_live_route`
(was 0 memories / 30 083 ms; now 1 memory / 1.8 s) [M, V].

## 10. MCP

17 tools, high-level only; no SQL/Cypher/shell/filesystem; no field that names a user or project;
no LLM dependency in the adapter [V: adapter tests, 50 pass]. **HIGH defect found and fixed:** `get_source`
(`ref`) and `get_agent_session` (`session_id`) were interpolated into the URL path, so `../../auth/me`
could leave `/projects/current/` (reproduced). Fix: per-segment validation (rejects `/ \ ? # %`,
control characters, `.`/`..`) plus a client-level check that every request path stays plain;
refusals are tool errors and send nothing. 22 new tests (10 hostile ids × 2 path-interpolating tools, a client-path test, an ordinary-id test) [V]. A project token would
still have been rejected by the engine outside its project, so this was an allow-list escape, not a
credential escape [I].

## 11. Coding-agent architecture

Agent → MCP/REST with project token → raw event → deterministic projection → queryable history →
resume. Evidence labels: git, agent_stated, parser, derived. Agent-stated memories are never treated
as Git fact; proposed updates are capped at confidence 0.8. [V]

## 12. Continuity

Resume returns unfinished work only from explicit evidence; with Git history but no sessions it
returns "nothing to resume" (scenario step 5); after session 1 it reproduces exactly the recorded
blocked task, decisions, attempts, changed files and tests (steps 17–22) [V]. It never infers
failed attempts or unfinished work. Real model use of this is a separate question (§23).

## 13. Git ingestion

Idempotent on SHA; structure facts superseded when stale; collector is read-only and local.
**HIGH defect found and fixed:** concurrent requests for the same commit raced on deterministic memory
creation; the loser's `ConflictError` was treated as a deterministic rejection, so the commit was
marked terminal `skipped` and its memory never created (2 of 3 runs reproduced it). Fix: the loser
adopts the existing memory, and conflicts are retryable. Regression test fires one request per commit
concurrently: 4/4 runs pass [V].
**[L]** Branch/structure reverts are not re-applied.

## 14. Provenance

Stored, not generated. UI now labels `git_commit` and `agent_event` sources with an accurate
explanation instead of "Reference as recorded" and no longer says projected memories were "added
directly" [V: unit test]. **[L]** Project-event and Git memories are absent from the review queue.

## 15. Temporal behaviour

Half-open intervals (`valid_from` inclusive, `valid_until` exclusive) are tested in
`test_lifecycle.py`, `test_graph_exploration.py`, `test_memory_service.py` (adjacent windows have no
overlap and no gap) and the retrieval/graph-intelligence suites [V]. Superseding closes the old
memory; current and historical answers differ correctly. **[L]** Not re-probed by a fresh live
query in this phase beyond the scenario's history/as-of steps.

## 16. Security

Isolation by `user_id` everywhere (audited); hashed, scoped, expiring tokens; security headers and CSP
(Playwright `csp` spec) [V]; rate limiting is active (a controlled measurement hit HTTP 429 once
the per-minute class limit was exhausted, as designed) [M]; prompt-injection boundary: retrieved
text is data and carries a notice [V]. Secret scan of tracked files: see §28.
Fixed: CORS did not allow `X-Memory-Space`, so a cross-origin deployment of the frontend would
fail every space-selected request; now allowed and tested [V].
**[L]** Account-session CSRF is covered by tests, not re-proved live (see §7).

## 17. Reliability and PostgreSQL ↔ Neo4j consistency

No distributed transaction. Event/Git projection is crash-safe (raw row first, deterministic ids,
adopt-existing) [V]. **Poison-event defect found and fixed:** an unexpected non-`AppError` exception
rolled back without counting an attempt (infinite retry) and an exhausted event kept every later
event of its session `pending`; now it counts an attempt, becomes `failed` at the limit, and later
events project (regression test) [V].
Documented, not fixed: embedding backfill is unscheduled and would embed structural memories;
review reconciliation has gaps; entity-merge retry is not idempotent; one bad extraction candidate
abandons its job; the semantic-duplicate path ignores superseded memories; documents stay
EXTRACTING after a crash; a PG row can outlive a lost Neo4j write for conversation/document flows [I].

## 18. Performance

Controlled measurement on the live stack, single client, small dataset (one scenario project,
97 Neo4j nodes), 25 sequential requests per case (first 2 dropped), local machine, fake LLM. **[M]**

| Request | p50 ms | p95 ms | Errors | Bytes |
|---|---|---|---|---|
| project memory search | 20.9 | 27.3 | 0 | 161 |
| project entity (file) | 28.5 | 34.6 | 0 | 13 247 |
| project graph | 34.2 | 44.2 | 0 | 11 778 |
| project history | 31.4 | 73.9 | 0 | 6 808 |
| project decisions | 10.0 | 14.9 | 0 | 1 317 |
| project sessions | 8.3 | 10.5 | 0 | 661 |
| resume (project token) | 21.2 | 28.9 | 0 | 5 919 |
| graph overview (space) | 35.2 | 47.7 | 0 | 21 719 |
| memory search (space) | 30.1 | 75.3 | 0 | 9 379 |
| hybrid search (space) | 29.2 | 37.4 | 0 | 12 (empty result) |
| auth/me | 3.7 | 11.0 | 0 | 284 |
| health | 1.3 | 1.9 | 0 | 34 |

Limits of this measurement: small data, one client, no concurrency, not a load test; the hybrid
search returned an empty result (the personal space of that account is empty), so its latency is the
no-hit path. MCP round trip and Git ingestion/projection timings are exercised by the scenario but
not separately benchmarked here. An initial un-paced run exceeded the per-minute rate limit and was
discarded (429s are not latency). **No optimisation was made;** nothing measured justified one.
Earlier scale/load results remain in `PHASE15_*`/`PHASE16_*` docs.

## 19. Frontend audit

Walked the running frontend in Playwright (13 areas). Found and fixed: `/search` hydration error
(`<details>` inside `<p>`); the graph and other page state surviving a memory-space switch (main
content is now keyed by the selected space); provenance gaps (§14). Typecheck, lint and 96 Vitest tests
pass; E2E in §28. `error.tsx` `retry` is correct for this Next version (checked against the bundled
docs). **[L]** Account-level pages (settings) are the same across spaces by design.

## 20. API audit

88 route/method pairs; every one has a contract test; the unused-by-frontend and unused-by-MCP
routes are expected (internal, auth, agent tooling). Errors use the common envelope with `request_id`.
Fixed: `X-Memory-Space` in CORS. **[L]** `docs/API.md` header was stale (now marked historical).
Duplicate/overlapping routes (e.g. `/entities/{id}/graph` vs `/graph/entities/{id}/neighborhood`)
are kept for compatibility.

## 21. Documentation audit

Corrected only factual errors: README unit-test count and ADR range, Phase 17 section; API.md header.
Not changed: older phase plan files are historical records. `render.yaml` and `DEPLOYMENT.md` refer to
`docs/DEPLOYMENT_LOG.md`, which does not exist because staging was never deployed — intended. 
**[L]** Unused settings remain: `document_ingestion_enabled` (no code path reads it, so it is not a kill
switch), `agent_max_context_memories`, `groq_api_key`, `extraction_schema_version`.

## 22. Evaluation audit

See `PHASE17_EVALUATION_MAP.md`: deterministic mechanics vs real-model behaviour kept separate.
**[L]** CI runs lint on `app tests ../scripts` only; database-backed suites, deterministic evals and
adapter tests run only in the opt-in/weekly workflow; `scripts/check_consistency.py` lacks Phase 16
coverage. A unit test (`test_observability::test_metrics_report_requests_errors_and_latency`) depends on the
ambient `backend/.env`: it passes with the suite's explicit environment and fails when run bare in a
developer shell with the dev `.env` — a test-isolation weakness (LOW).

## 23. Real-agent test status

One real-agent run was performed in Phase 16 and reported there; it was **not repeated** in Phase 17
and no Claude/Anthropic usage was consumed. **Real-agent rerun recommended**, because the agent write
path and the MCP guard changed afterwards. Not fabricated, not assumed.

## 24. Issues found, fixed, deferred

| # | Severity | Issue | Status |
|---|---|---|---|
| 1 | HIGH | MCP path traversal via `ref` / `session_id` | fixed + tests |
| 2 | HIGH | Concurrent Git projection silently lost commit memories | fixed + regression test |
| 3 | HIGH | Agent writes deadlocked on the conversation row lock (30 s, write lost) | fixed + regression test |
| 4 | MEDIUM | Poison event retried forever / exhausted event blocked its session | fixed + test |
| 5 | MEDIUM | Graph/page state survived a memory-space switch | fixed (keyed remount) |
| 6 | MEDIUM | Provenance panel did not explain git_commit/agent_event; "added directly" wording | fixed + test |
| 7 | MEDIUM | CORS missing `X-Memory-Space` | fixed + test |
| 8 | LOW | `/search` hydration error | fixed |
| 9 | LOW | Unit test depends on ambient `.env` | deferred |
| 10 | MEDIUM | Embedding backfill unscheduled / would embed structural memories | deferred |
| 11 | MEDIUM | Document EXTRACTING after crash; entity-merge retry non-idempotent; bad candidate abandons job | deferred |
| 12 | LOW | Dead settings (`document_ingestion_enabled` …) | deferred |
| 13 | MEDIUM | Project-event/Git memories absent from review queue | deferred |
| 14 | INFORMATIONAL | CI coverage gaps | deferred |

Data cleanliness (M24): 497 `eval-agent-*` Neo4j nodes at audit time (~85 % of the dev graph; 535 after this phase's eval runs) are scoped by
`user_id` so no other user sees them, but they inflate admin sweeps. **Not deleted.** Safe options,
all needing explicit approval: delete by `user_id` prefix `eval-agent-` in Neo4j and PostgreSQL after a
dry-run count; or move them to a separate database. An unexplained `itest-72b98e5d…` leftover (7
memories) is likewise left. Phase 17's own synthetic data (`p17-*` accounts and their projects)
is removed with `eval/system_scenario_cleanup.py` (dry run by default). Phase 16 synthetic data stays clean: the only
accounts in the dev database are the real account and the `p17-*` ones (checked by query) [V].

## 25. Remaining architectural risks

1. No distributed transaction between PostgreSQL and Neo4j for conversation/document flows.
2. Three ranking/term implementations that can drift.
3. Project retrieval relies on SQL scans without trigram/GIN indexes — fine at measured sizes only.
4. No cross-space search: an agent cannot link a personal fact to a project fact (accepted).
5. Real-model behaviour (extraction quality, tool choice, benefit from continuity) is under-measured.
6. Production readiness is not claimed: staging was never deployed.

## 26. Product understanding

What it is: durable, inspectable, temporal memory that an agent can query and trust to the extent
its provenance allows. What is strong: governance (no silent overwrite or merge), explicit-evidence
continuity, isolation by construction, deterministic project path with no LLM. What is thin: real-model
evidence, scale, operational maturity. It is one coherent product with two doors (personal memory and
project memory) over one engine; the seams are the duplicated concepts in §4/§8, not separate systems.

## 27. Final end-to-end scenario result

`backend/eval/system_scenario.py`: **23/23 steps passed** over the live stack (engine on 8001) using a real
stdio MCP connection, the real Git collector and two synthetic accounts. A first run failed 10 steps
because the script stamped events in the future and the engine correctly rejected them; the script was
fixed (not the engine) and the rerun passed. No step was faked; the "agent" is a script, so this is
mechanics, not model behaviour (§23).

## 28. Regression results (M28)

All numbers below come from runs in this phase [V].

| Check | Result |
|---|---|
| Backend full suite (`pytest`, explicit test environment: test PostgreSQL, live Neo4j, fake LLM/embeddings) | 2209 passed, 0 failed, **59 errors** — all at fixture setup in 9 integration files, cause `Neo.ClientError.Security.AuthenticationRateLimit` (Neo4j's own limiter tripped by repeated bad-credential attempts from bare-environment runs I made while the suite ran). The same 9 files rerun in isolation: **148 passed**. No test failure was observed. |
| New regression tests (`test_phase17_regressions.py`, poison-event test, CORS test) | pass; Git race test 4/4 repeated runs |
| MCP adapter tests | 50 passed |
| Frontend: `tsc --noEmit`, `eslint`, Vitest | clean; 96 passed |
| Frontend production build + Playwright (backend on a test DB, `next start`) | **40 passed** (auth, CSRF, CSP, a11y/axe, documents, graph, isolation, memory, projects/space switching, review) |
| Deterministic evals (`graph_api` 39/39, `retrieval`, `document`, `runner`, `memory_quality`, `graph_intelligence`, `project_memory`, `agent`) | all exit 0; no failures reported |
| System scenario on the live stack with the fixed code | 23/23 |
| `ruff check app tests eval ../scripts` | clean |
| `alembic heads` / `alembic check` | single head `b2f392141c1b`; no new upgrade operations |
| `npm audit --omit=dev`, `pip-audit` | 0 vulnerabilities / none known |
| Secret scan of tracked files (key patterns, `sbp_` tokens, private keys, tracked `.env`) | no matches |

Notes: (1) the first full-suite attempt's `.env`-dependent unit test passes only with the explicit
environment (§22). (2) The Neo4j limiter incident is environmental, but it shows that bad-credential
runs against the shared Neo4j can break an unrelated suite run. (3) Running the deterministic
evals writes `eval-agent-*` nodes to the shared Neo4j (535 now, 497 before this phase's runs): the
pre-existing accumulation noted in §24 grows with each agent-eval run.
**Not run:** the real-LLM evaluations and the real external agent (§23).

## 29. Final health check (M29)

Live stack: backend on 8001 (restarted from the fixed code, with local-launch settings
`FRONTEND_URL=http://localhost:3000` and raised login/register limits so the scenario can create accounts), frontend
dev server on 3000 (restarted after I stopped it by mistake).

| Item | Result |
|---|---|
| Backend starts; `/health` | `ok`, version 0.11.0 |
| `/health/ready` | neo4j ok, postgresql ok, migrations ok |
| Migrations | at head `b2f392141c1b` |
| Frontend starts | `/login` 200 |
| Auth, project isolation, MCP, retrieval, documents, agent, coding-agent flow, resume, graph explorer | exercised by the 23-step scenario (auth, isolation, MCP over stdio, Git, events, search, resume), by Playwright (graph explorer, documents, review, isolation) and by the agent eval; documents and agent are not re-driven live with a real model |
| Synthetic data | 7 `p17-*` accounts, 3 projects/spaces and their 155 Neo4j nodes removed with `eval/system_scenario_cleanup.py`; verified 0 accounts, 0 spaces, 0 leftover nodes |
| Real data / `eval-agent-*` | untouched

## 30. Next steps

1. Approve a one-off real-agent rerun to re-measure continuity after these fixes.
2. Decide on the `eval-agent-*` cleanup option (§24).
3. Schedule or remove the embedding backfill; exclude structural memories if kept.
4. Make `ci.yml` lint `eval/` and `integrations/mcp/`; add adapter tests to push CI.
5. Unify ranking/term helpers only if a measured divergence appears.
