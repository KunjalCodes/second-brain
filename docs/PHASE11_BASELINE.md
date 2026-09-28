# Phase 11 — M0 Baseline (re-verification, 2026-09-28)

## Context

Phase 11 was implemented and committed on 2026-09-27 as `18c93a9` ("chore:
harden system for staging deployment"). Its full report is
`docs/PHASE11_PLAN.md` §31.

Phase 12 exists **only as a plan** (`docs/PHASE12_PLAN.md`, uncommitted). No
Phase 12 code is in the repository.

This document is the M0 baseline for a **re-verification pass** over the
current tree. It was taken before any code changed in this pass.

**Labels:**

| Label | Meaning |
|---|---|
| [V] | Verified by a check that ran now |
| [M] | Measured now |
| [I] | Inspected in code |
| [A] | Assumption |
| [L] | Limitation |

**Conditions.**
- Apple M1, local PostgreSQL 17, and local Neo4j Community 2026.08.1.
- The test database was `ai_second_brain_test`.
- LLM and embeddings used the `fake` provider.
- The machine was **not idle**: load average 7–10, with a browser tab using
  about 60% CPU. Absolute timings are therefore noisier than the 2026-09-27
  run [L].
- The user's dev servers (ports 8001 and 3000) were stopped during
  measurement.

## 1. Git state [V]

- `HEAD` is `18c93a9`.
- Uncommitted when this baseline was taken:
  - `backend/app/llm/providers/gemini.py`, `backend/app/extraction/runner.py`,
    `backend/tests/unit/test_llm_gemini.py`,
    `backend/tests/unit/test_extraction_repair.py`. These are two LLM fixes
    found while loading demo data against real Gemini on 2026-09-28; see the
    report §5.
  - `docs/PHASE12_PLAN.md`.
- No remote exists.

## 2. Tests and checks [V]

| Check | Result |
|---|---|
| Backend full suite | **1418 passed**, 0 failed, 126 s |
| Frontend unit (Vitest) | **69 passed** (7 files) |
| Frontend typecheck / lint | OK / OK |
| E2E (Playwright + axe, production build) | **29/29** |
| Ruff (`app tests eval ../scripts`) | Clean |
| Secret scan | 278 files, 0 findings |
| pip-audit (runtime deps) | No known vulnerabilities |
| npm audit (prod, high+) | 0 vulnerabilities |
| `alembic heads` / `alembic check` | 1 head (`320f6d898a92`) / no new operations |

## 3. Deterministic evaluations [V]

| Harness | Result |
|---|---|
| Graph API conformance | 39/39 |
| Retrieval (3 baselines) | Mean Recall@K 91.7% / 68.8% / 100%; output identical to the `a9da2db` comparison run of 2026-09-27 |
| Document | 10/10 PASS |
| Agent | Real-LLM section skipped (`LLM_PROVIDER=fake`); structural prompt-injection check PASS |

## 4. Backend load, local [M]

The load test is `eval/load_test.py`, run against a single uvicorn process
with rate limits on but set high. The run is 8 s after 2 s of warm-up.
Values are p50 / p95 in ms, with throughput in rps. Every run had a **0%
error rate**.

| Endpoint | c=1 | c=10 | c=25 | c=50 |
|---|---|---|---|---|
| `GET /auth/me` | 3.0 / 3.9 (304) | 13.3 / 21.3 (709) | 32.7 / 175.5 (461) | 71.1 / 459.5 (371) |
| `GET /memories/search` | 12.5 / 16.4 (76) | 56.1 / 78.5 (170) | 141.3 / 179.0 (174) | 270.7 / 519.4 (174) |
| `GET /graph/overview` | 16.8 / 22.9 (56) | 68.8 / 95.3 (140) | 175.4 / 212.1 (140) | 338.6 / 573.4 (144) |
| `GET /graph/entities/{id}/neighborhood` (2 hops) | 16.7 / 22.7 (57) | 65.5 / 90.7 (147) | 172.9 / 245.4 (139) | 357.4 / 601.8 (134) |
| `GET /graph/edges/evidence` | 13.5 / 17.8 (70) | 59.1 / 77.2 (161) | 154.5 / 186.2 (160) | 298.9 / 483.7 (162) |
| `POST /search` (hybrid) | 23.4 / 28.4 (42) | 106.3 / 156.1 (89) | 258.9 / 342.7 (96) | 521.6 / 860.6 (94) |
| `POST …/agent-turn` (offline fake LLM) | 23.8 / 28.8 (41) | 250.8 / 348.4 (44) | 596.6 / 670.9 (42) | 1253.8 / 1340.5 (40) |

**Login** (scrypt), p50 / p95: 300 / 309 ms at c=1, 392 / 401 at c=5, and
562 / 746 at c=10.

**Notes:**
- `edge_evidence` and `agent_turn` are new scenarios in this pass, so there
  is no earlier figure to compare with.
- **Agent turns are serialised per conversation and the conversation grows
  during the run.** Every virtual user appends to the same conversation, and
  each turn re-reads up to 1000 messages. So these figures describe a single
  busy conversation, not independent users [L]. They also never include a
  real model's latency.
- **Throughput plateaus at c≥10** for every endpoint. The service is a
  single process; this is queueing, not errors [I].

## 5. Jobs and ingestion [M]

Measured with `eval/job_benchmark.py`, fake LLM:

| Measurement | Result |
|---|---|
| Extraction | 60 jobs in 4.27 s = **842 jobs/min**; per-tick p50 69 ms, p95 94 ms |
| Claim contention | 10 claimers, 100 jobs → 100 claimed, **0 duplicates**; claim p50 9.3 ms, p95 41.8 ms |
| Ingestion | 10 documents in 0.47 s = **1265 docs/min**; p50 47.6 ms per document; 90 chunks |

## 6. Frontend [M]

Measured with `e2e/perf.spec.ts` against a production build. Medians of 5
unless stated.

| Measurement | Result |
|---|---|
| `/memories` cold JS, critical (before `load`) | **168,462 B** transferred |
| `/memories` cold JS, prefetch | 30,912 B |
| `/login` JS, critical | 157,656 B (+ 5,826 B prefetch) |
| LCP `/memories` (authenticated) | 176 ms |
| LCP `/login` | 116 ms |
| Auth bootstrap → first page | 159 ms |
| API from the browser, median ms | memory_search 27, memory_detail 12, provenance 11, graph_overview 57, neighborhood 2-hop 24, documents 8, hybrid 102 |
| Graph canvas render at the budget (150 nodes / 149 edges, truncation banner shown) | 610 ms |

**Graph end-to-end attribution** (median of 3, ms):

| Size | Elements | Backend | Network | Layout | Paint |
|---|---|---|---|---|---|
| ~50 nodes | 99 | 67 | 3 | 101 | 35 |
| ~150 nodes | 299 | 112 | 4 | 613 | 46 |
| ~300 nodes (after expansion) | 597 | 98 | 6 | **2224** | 48 |

**Layout is the bottleneck**, as on 2026-09-27. That run measured 1550 ms at
597 elements. The difference here is attributed to machine load, since the
code is identical [A].

**Harness fix made during M0.** The attribution test did 3 sizes × 3 trials
and hit Playwright's 30 s default timeout once. Rerun, it took 24.6 s. It now
sets `test.setTimeout(120_000)`. This changes only the measurement, not the
product [V].

## 7. Behaviour already verified by tests

Each item below was confirmed [V] by the suite run in §2:

- infrastructure failure is retried, not INVALID, and each memory is stored
  exactly once;
- a stale worker cannot overwrite a new owner;
- the last failed attempt is terminal and visible;
- a crash during parse is recovered by the sweep;
- the FK-blocked retry path;
- missing bytes fail the document explicitly;
- the PDF timeout is terminal;
- embedding backfill goes beyond the first page;
- restart recovery;
- a duplicate sweeper is skipped by the advisory lock;
- graceful stop, both waiting and cancelling;
- dead-session cleanup;
- LLM throttling defers without consuming attempts;
- no LLM failure writes to Neo4j;
- error leaks;
- log redaction;
- upload limits (valid, at limit, over, declared oversize, streamed
  oversize, malformed, truncated, and 200 MiB bounded memory);
- security headers;
- production config (all problems reported at once);
- readiness, including migration mismatch;
- migrations on fresh and existing databases;
- contract inventory;
- real-LLM evaluations opt-in only.

## 8. Gaps found by re-inspection (inputs to this pass)

| # | Finding | Label |
|---|---|---|
| G1 | Production CSP `script-src` allows `'unsafe-inline'`. That is not a strict CSP. Next.js 16 supports per-request nonces with `'strict-dynamic'` through `proxy.ts` (`node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md`) | [I] |
| G2 | The load test had no edge-evidence or agent-turn scenarios; added for this baseline | [I] |
| G3 | The graph attribution test was flaky against the 30 s timeout; fixed (§6) | [V] |
| G4 | Found live on 2026-09-28: real Gemini rejected every extraction request (HTTP 400, "schema … too many states"), and the repair retry did not say what was wrong. Both are fixed in the working tree (uncommitted) | [V] |
| G5 | The agent prompt labels its data sections but does not fence them the way the extraction and document prompts do | [I] |
| G6 | Readiness is served at `/health/ready`, not `/readiness`; Render's health check uses `/health/ready` | [I] |
