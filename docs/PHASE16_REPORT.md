# Phase 16 — Coding-agent memory integration: Report (2026-10-02)

**Scope.** Turn the memory engine into a provider-agnostic project-history and agent-continuity
system that external coding agents use through MCP. The agent keeps its own model, provider and API;
the engine stores, retrieves and explains. No LLM is called by any new path.

- **Baseline:** `510456c`. Designs: `PHASE16_ARCHITECTURE.md` (M0 audit and design, with an "as built" section),
  `PHASE16_PROJECT_MEMORY.md`, `PHASE16_MCP.md`, `PHASE16_AGENT_CONTINUITY.md`, ADRs 0034-0036.
- **Labels:** [V] verified by a test or check that ran; [M] measured; [I] inspected in code; [A] assumption; [L] limitation.
- **Conditions and non-claims.** Everything is local (Apple M1, PostgreSQL 17, Neo4j Community, deterministic fake
  embeddings, no LLM). The machine was shared and under memory pressure during parts of the run, so latencies are
  orders of magnitude. **This is not a production-readiness claim.** Nothing was deployed or pushed.

## Verification summary [V]

| Check | Result |
|---|---|
| Backend suite | **2264 passed, 0 failed** (522 new tests across 11 new files; the audit tests for routes, SQL and Cypher were extended) |
| MCP adapter | 28 passed (separate package) |
| Frontend | typecheck clean, lint clean, **95 unit tests** passed |
| E2E (production build, real stack) | **40 passed** (39 + the new project-space test) |
| Evaluations | graph conformance 39/39; retrieval, document, extraction, memory-quality, agent (structural) pass; graph intelligence 28/28; **project memory 30/30** |
| `ruff check app tests eval ../scripts` | clean (`ruff format` was applied only to new files) |
| Secret scan | 298 files, 0 findings |
| `alembic check` | no new upgrade operations; single head `b2f392141c1b`; downgrade/upgrade round-trip tested |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `pip-audit` | adapter: none; backend: 3 advisories on transitive `urllib3 2.7.0` (pre-existing, not changed) |
| Real external agent | performed once (§20); not re-run after the fixes it motivated |

Failures found by verification and fixed on the way: `MemoryService.create` id reuse collision; a NUL byte in a query
topic or session id caused a 500 (now 422/404); resume `task` acted as a filter; the adapter dropped validation details;
session recency used server time; an a11y test relied on `networkidle` (a navigation prefetch artefact).

## 1. Before / after architecture

Before: one memory space per account (`user_id`), memory created from conversations and documents by an LLM
extraction pipeline, no notion of projects, repositories, sessions or machine credentials. After:

```text
External agent ─MCP/stdio─▶ integrations/mcp ─Bearer sbp_─▶ /projects/{id}/… ─▶ Postgres (events, sessions, commits)
                                                                              └▶ Neo4j (existing graph, per-project space)
local git ──scripts/git_collect.py (read-only)──▶ /projects/{id}/git/…
```

Schema: two additive migrations (`projects`, `project_tokens`; `agent_sessions`, `agent_events`, `git_commits`),
no existing table altered, no new Neo4j label or index [V: `alembic check`, migration round-trip test].

## 2. MCP implementation [V]

`integrations/mcp/` (separate package, `mcp` 2.2): 17 tools, stdio, one project token from the environment, every
call to `/projects/current/…`. No tool accepts a user, project, space, Cypher, SQL, command or path outside the
repository; engine errors reach the agent as `code (HTTP n): message. Fields: …` with no token, header or request id.
**28 adapter tests** (surface, bounds, token handling, error hygiene, oversized/non-JSON responses, redirects off).

## 3. Project model [V]
Projects are isolated memory spaces (ADR 0034); identity is a normalised remote URL or root commits; filesystem
paths are refused; registration is idempotent and race-safe (6 concurrent registrations → 1 project); per-account
and per-project caps. Scoped, hashed, expiring, revocable tokens (ADR 0036): shown once, never listed, never logged.

## 4. Git ingestion [V]
`scripts/git_collect.py`: read-only fixed-argument `git`, no shell, no diffs/contents/raw emails/credentials; commits,
parents, branches, changed files with status and add/delete counts, Python structure via `ast`. 52 collector tests on
real temporary repositories, including hostile file names, unsafe revisions and agreement with the server's path rules.
Server: idempotent by hash; identity-mismatch guard; structural memories are not embedded.

## 5. Agent session model [V]
`agent_sessions` (client key, valid-time recency, outcome, summary) and an append-only `agent_events` log with a
server-assigned recording order. Sessions continue earlier sessions explicitly (`continues_session`); an ended
session accepts no new events but replays of old events still succeed.

## 6. Event ingestion [V]
13 typed events + `memory_proposal`; every payload `extra="forbid"`, cleaned, bounded; no authority fields; atomic
validation per request; time-skew bounds; per-session cap. 65 unit tests on the boundary.

## 7. Memory write pipeline [V][I]
Event → `agent_events` → deterministic candidate → the **existing** governance pipeline
(`ExtractionRunner.apply_structured_candidate`: grounding, ambiguity flagging, duplicate / conflict / supersession,
provenance) → canonical memory. Git/parser evidence is observed and immutable, so it uses deterministic ids and exact
identity without classification. Structural entities are exact-identity (no fuzzy flagging, no embedding quota).
Finding: `MemoryService.create` could not safely take a caller id; it now takes an optional `memory_id` [V].

## 8. Project graph [V]
14 new entity types, 2 new source types, project predicates registered as multi-valued; only evidenced edges.
Explorer shows project spaces (switcher), the new types in the type filter and per-type shapes (§15).

## 9. Retrieval [V][M]
Search reuses `HybridRetrievalService` over the project's space (evidence-labelled); history, file, commit, decision,
bug, error, fix and attempt queries are deterministic Postgres reads with cursor paging; the entity and graph views
reuse the Phase 15 services under hard caps (60 nodes, 2 hops). 17 read routes + 1 write.

## 10. Resume context [V][M]
`get_resume_context`: dedicated, bounded, deterministic (see `PHASE16_AGENT_CONTINUITY.md`). Works with Neo4j down
(supporting memories empty, everything else intact) and with no LLM.

## 11. Agent continuity [V]
Session chains survive a session that only said "continue"; unfinished work is explicit-only (3 bases); the active task
is the newest unclosed request; sessions are ordered by the agent's own valid time.

## 12. Previous-attempt memory [V]
Attempts exist only where the agent labelled an `approach` or recorded a fix; the outcome is the newest stated fix
outcome, else the newest test result, else `blocked`, else `no_outcome_recorded`. File changes alone never create an
attempt (tests assert it, including repeated modifications and commits).

## 13. Decision history [V]
Decisions with `EXPLICIT_DECISION` rationale only when stated; `OBSERVED_OUTCOME` entries carry their basis; supersession
is explicit, bidirectional, keeps both memories and closes the old one's validity at the new decision's time.
`INFERRED_REASON` is reserved and never emitted.

## 14. Bug / error history [V]
Bug status from stated fix outcomes (`open`, `fixed`, `partially_fixed`, `fix_attempted_failed`,
`error_recorded_after_fix`), validating tests only if a passing run names the fix; errors grouped with the fixes recorded
for them (`fixed_by` only for stated `succeeded`).

## 15. Graph Explorer changes [V]
Extended the existing Explorer: memory-space switcher (clears the cache on switch; a stale stored project falls back
to personal memory), the 14 new entity types in the filter, per-type node shapes (not colour alone), the
`X-Memory-Space` header on data routes only. No second graph UI. **Not built:** dedicated session, date-range, branch and
confidence filter widgets (the time lens, entity picker and the history API cover those needs) [L].
Frontend: typecheck and lint clean, **95 unit tests** (86 + 9 new); E2E in §19.

## 16. Authentication and security [V]
Project tokens cannot act outside `/projects/{id}/…`, cannot mint or revoke tokens, never reach session routes; the
header `X-Memory-Space` is ignored on project routes; revoked/expired/forged/malformed → one generic 401; disabled owner
disables tokens; rate limits per owning account. Dedicated audit: `tests/integration/test_project_security.py` —
Cypher/SQL/template/traversal payloads against every query parameter and id, hostile text as data, size bounds,
secrets in responses and logs, authentication edges. It found and fixed: NUL bytes in query text and session ids
reached PostgreSQL as a 500 (now 422/404). The server's project modules import no file or process APIs (tested).

## 17. Isolation tests [V]
Cross-project (same account), cross-user and forged-id probes on **every** read route (account session, token of another
project, anonymous, stranger), plus ingestion, resume, sources and proposals. The evaluation's leak probe: 0 leaks.

## 18. Idempotency tests [V]
Same event twice; same batch twice; partially repeated batch; reused id with different content → 409; replay after the
session ended; concurrent identical requests; retry after Neo4j failure; crash after Neo4j commit (adopted, not duplicated);
concurrent projection of one event; sweeper retry; same hash different content; commit replays at any batch size; structure
re-ingest, changed-file supersession, older-after-newer snapshot. Evaluation: all deltas 0.

## 19. Deterministic evaluation [M]
`eval/project_memory_runner.py`: a real temporary git repo, the real collector, a scripted history (an unrelated
distractor session, then a two-session fix-and-continue scenario) through real routes with a project token. 30 tasks over
categories A-S plus search.

| Result | Value |
|---|---|
| Tasks passed | **30/30** (A-S all pass, plus 3 search tasks) |
| Retrieval precision / recall | 1.0 / 1.0 over the set-valued checks (file history commits/sessions/decisions, decisions, attempts, unfinished work); small sets, so a regression net, not an accuracy claim |
| Provenance accuracy | 25/25 sampled memories resolve (all sources resolved, expected source type present) |
| Temporal accuracy | valid-time window returns only that era; valid and recorded time differ on every event; superseded decision closed at the new decision's time |
| Idempotency | row and memory deltas after replaying commits, structure and every event: **0 / 0 / 0 / 0** |
| Isolation | **0 leaks** across 7 probe routes (other project, same account); cross-project token → 404; cross-user → 404 on read, resume and ingest |
| Resume correctness | decisions, attempts, changed files, previous session and explicit unfinished items all present |
| Unnecessary context | **0 of 6 items (0.0)**; **before** the relevance gate it was 3 of 9 (33.3%) |
| Median latency (ms) | resume 83.9, file history 149.5, commit context 64.7, decisions 23.3, attempts 29.6, session 37.1, search 37.8, graph (2 hops) 32.3, provenance 14.7 |
| Median payload (bytes) | resume 6954, file history 2688, commit context 4121, decisions 3352, attempts 3238, session 6810, search 4288, graph 24822 |

Dataset caveat: authored alongside the code it scores, one scenario with one distractor session. Two honest corrections
happened during the work: the file-history expectation for the distractor file was wrong (no file event existed) and was
fixed in the dataset; the provenance check was changed to require that *every* source resolves and the expected type is
*present* (the first version required the first source to be of the expected type).


## 20. Real external-agent evaluation — **performed**; result is mixed and informative [M][L]
Run explicitly, once, with the local `claude` CLI on the user's own claude.ai-authenticated access (no other paid
service; no credential stored in the engine; engine LLM = none). Exactly two bounded sessions against a throwaway engine
and a toy repository (`eval/real_agent_continuity.py --run-real-agent`): session 1 "Investigate authentication and fix
the session timeout bug" (max 30 turns); session 2, a new process with no history, "Continue where we left off." (max 8).
Reported cost (estimate): $0.90 + $0.21.

**What happened (from the saved transcripts):**
- Session 1 fixed and committed the bug in the toy repo. It tried 32 `record_agent_event` calls: **9 accepted, 23 rejected
  (72%)**. The tool took a free-form `payload`, the agent guessed field names, and the engine's field-level errors were
  dropped by the adapter, so it could not self-correct. It never managed `user_request`, `decision`, `commit` or
  `session_ended` (so no `unfinished` list). It hit the turn limit.
- Session 2 first passed the id of its *new* session to `get_resume_context` (404), then called it without. The engine
  returned exactly what had been recorded: last session (with its goal, which mentioned the README note), changed file,
  all three test runs, both commits. It **correctly returned no invented unfinished work**. It omitted the three recorded
  bugs and the recorded fix: the `task` argument ("auth-service continue work") was applied as a whole-phrase filter.
  Session 2 then continued with the README note.
- Item recall against what session 1 actually recorded: session start 1/1, files modified 1/1, test runs 3/3, bugs 0/3,
  fix 0/1 → **5/9 (56%)** at the time.

**Defects this exposed, and fixes** [V by tests, **not re-validated with a new real-agent run**]:
1. The adapter now surfaces `details.fields` validation errors, `record_agent_event` documents every payload shape, and
   `get_resume_context` says `session_id` must be an existing session.
2. `task` is now a relevance bias, never a filter. A regression test replays the 9 events the real agent managed to record
   and resumes with the same task string: the bug and the fix are returned, no unfinished work is invented.

**Limitations of this evaluation:** one run, one agent, a toy repository, a protocol supplied through a system prompt (a
wrapper integration would normally do that), and I judged the engine's output, not the model's. It shows the integration
works end-to-end and that integration friction (tool schemas) matters more than retrieval. It does **not** show that a
future agent will record good events, nor that the fixes restore the full recall; re-running the same two sessions is the
direct check and spends further usage, so it was not repeated without your say-so.

## 21. Performance [M]
Synthetic, in-process, median of 5, one Neo4j Community instance, fake embeddings, a loaded machine.

| Files (commits, sessions) | commits/s | structure files/s | events/s | resume | file history | commit ctx | history (50) | search | graph 2 hops (≤60) |
|---|---|---|---|---|---|---|---|---|---|
| 100 (100, 30) | 17.2 | 14.7 | 6.3 | 47 ms / 5.0 KB | 25 ms / 2.2 KB | 17 ms / 1.5 KB | 18 ms / 18 KB | 340 ms / 10 KB | 152 ms / 73 KB |
| 1,000 (300, 30) | 10.9 | 12.8 | 6.3 | 45 ms / 5.0 KB | 24 ms / 1.2 KB | 16 ms / 1.6 KB | 17 ms / 18 KB | 468 ms / 10 KB | 262 ms / 49 KB |
| 10,000 (500, 20) | 14.6 | 24.5 | 2.0 | 84 ms / 5.2 KB | 35 ms / 1.2 KB | 20 ms / 1.6 KB | 19 ms / 18 KB | 417 ms / 11 KB | 280 ms / 53 KB |

- 10,000 files produced 11,701 graph memories; the whole 10,000-file structure ingest took about 8 minutes. Query latency
  barely moves with project size (resume 45 → 84 ms; file history 24 → 35 ms) because the Postgres reads are indexed and
  bounded; search and graph dominate and are dominated by the pre-existing hybrid retrieval and neighbourhood code.
- Ingestion is the slow side: one Neo4j transaction per memory, plus governance for agent events (2-6 events/s). Fine for
  interactive agent use; a bulk import of a very large history would take minutes.
- Max latencies were 3-8x the medians on the loaded machine (e.g. graph max 1.7 s at 10,000 files). Hard caps held at every
  size (≤ 60 nodes, ≤ 120 edges, ≤ 50 items, resume ≤ 5.2 KB here and < 40 KB in the 40-decision stress test).


## 22. Known limitations [L]
- Quality of continuity depends on what agents record; free-form `record_agent_event` payloads caused most real-agent
  friction. Typed per-event tools would be the next usability step.
- Conflicting agent proposals are stored `conflicting` but are not added to the human review queue.
- Python-only structure; no call graph; deleted files do not retract structure memories; reverting a file to a previous
  structure does not re-activate the old memory.
- No cross-space (personal + project) search; no project deletion (archive status only).
- Ingestion is one Neo4j transaction per memory: events 2-6/s, commits 11-17/s, structure files 13-25/s.
- Graph responses are large (50-73 KB at 60 nodes); history/attempts pages ~18-19 KB. MCP callers should lower `max_nodes`/`limit`.
- No wall-clock timeout on queries (row caps and bounded loads only); relevance in resume is simple (chain + file overlap +
  task words); no semantic relevance.
- Stdio MCP only; no OAuth; one token per adapter.
- Dependency audit: `pip-audit` reports 3 advisories on the transitive `urllib3 2.7.0` (fix 2.8.0) in the backend lock,
  which pre-date this phase; **not changed here** (no dependency bumps in scope). The adapter's dependencies: none found.
- The pre-existing `eval-agent-*` Neo4j nodes were left untouched.

## 23. Deferred work
Typed MCP tools per event type; streamable-HTTP/OAuth MCP; review-queue integration for agent conflicts; language
parsers beyond Python; project archive/delete; cross-space federation (read-only); retention policy for events; embedding
opt-in for structural memories.

## 24. Future integration possibilities
Other MCP clients (Cursor, Codex) need no engine change: they use the same REST contract (`PHASE16_MCP.md` §6). A
commit-hook or CI job can run the collector on a schedule; an agent wrapper can emit `session_started`/`session_ended`
automatically so the model only records decisions and outcomes.
