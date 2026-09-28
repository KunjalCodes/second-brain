# Phase 11 — Hardening Report (re-verification, 2026-09-28)

**Scope.** Phase 11 was first implemented on 2026-09-27 as `18c93a9`; the
full report for that work is `docs/PHASE11_PLAN.md` §31. This pass
re-checked that work against an expanded checklist (M0–M18) on the current
tree and fixed what the checks exposed.

**Phase 12 exists only as a plan** (`docs/PHASE12_PLAN.md`). No Phase 12
code was written or preserved, because none exists.

**Baseline.** `docs/PHASE11_BASELINE.md` records M0.

**Labels:**

| Label | Meaning |
|---|---|
| [V] | Verified by a test or check that ran in this pass |
| [M] | Measured in this pass |
| [I] | Inspected in code |
| [A] | Assumption |
| [L] | Limitation |

**Conditions and non-claims.**
- All results are local: Apple M1, PostgreSQL 17, Neo4j Community 2026.08.1,
  fake LLM and embeddings.
- There are **no cloud results, no real-LLM results and no staging
  results**.
- The machine was shared with other applications: load average 4–17 during
  the runs. This is reported wherever it affects a number.

---

## 1. Baselines

| Check | M0 (before) | After |
|---|---|---|
| Backend suite | 1418 passed | **1419 passed** (+1 regression test) [V] |
| Frontend unit | 69 | **72** (+3 CSP tests) [V] |
| E2E (production build) | 29/29 | **31/31** (+2 CSP tests) [V] |
| Typecheck / lint / ruff | OK | OK [V] |
| Graph conformance | 39/39 | 39/39 [V] |
| Retrieval evaluation | 91.7 / 68.8 / 100 | Byte-identical to M0 [V] |
| Document evaluation | 10/10 | 10/10 [V] |
| Agent evaluation | Real-LLM skipped; injection PASS | Same [V] |
| `alembic heads` / `check` | 1 head / clean | 1 head / clean [V] |
| Secret scan | 0 findings | 0 findings [V] |
| pip-audit / npm audit | 0 / 0 | 0 / 0 [V] |

## 2. Job correctness

Already present from `18c93a9`, and re-verified by tests [V]
(`tests/integration/test_job_reliability.py`):

| Behaviour | Test |
|---|---|
| Neo4j or infrastructure failure is retried and never marks a memory INVALID; the memory is stored exactly once | `TestNeo4jOutageIsRetryable` |
| Retry exhaustion is terminal and visible | `TestRetryExhaustion` |
| Legacy exhausted `failed` jobs are resolved by the sweep | `TestRetryExhaustion` |
| Ingestion recovers after a crash | `TestIngestionRecovery` |
| The FK-blocked retry path, fixed without weakening the constraint | `test_retry_after_chunk_jobs_committed_is_not_blocked_by_fk` |
| Missing file fails the document explicitly | `TestIngestionRecovery` |
| PDF parse timeout is terminal; a running parse defers the next | `TestParseTimeout` |
| Embedding backfill goes past the first page | `TestEmbeddingBackfill` |

**New in this pass [V]:**

**Agent context window bug (pre-existing, Phase 5).**
- *Cause:* the agent route read the **oldest** 1000 messages and took their
  last 10.
- *Effect:* once a conversation passed 1000 messages, the agent's context
  became messages 991–1000. The user's new question was **silently left
  out**.
- *Fix:* `MessageRepository.list_recent` (newest N, oldest first) and
  `ConversationService.recent_messages`.
- *Test:* `test_window_is_the_newest_messages_of_a_long_conversation`
  (1005 messages) fails when the old code is restored.
- *How it was found:* the new agent-turn load scenario made the conversation
  grow past 1000 messages.

**Real Gemini rejected every extraction request.**
- *Cause:* HTTP 400, "schema … too many states". The length, count, range
  and pattern bounds in `ExtractionResult` were too many for Gemini's
  constrained decoding.
- *When:* found live on 2026-09-28. Pre-existing and latent, because CI uses
  the fake LLM only.
- *Fix:* the adapter strips validation-only bounds from the schema it sends
  to Gemini. Pydantic still enforces every bound on the reply, so nothing is
  weakened.
- *Test:* `tests/unit/test_llm_gemini.py`.

**The repair retry now names the failing fields.**
- It lists field paths and messages only, never the offending values.
- *Test:* `tests/unit/test_extraction_repair.py`.
- *Live result:* Gemini's first replies broke the predicate pattern, the
  repaired retries succeeded, and those jobs completed [M, 2026-09-28].

## 3. Stale-worker protection

Already present [V] (`TestStaleWorker`):
- lease tokens per claim;
- conditional updates `WHERE locked_by = token AND status = 'in_progress'`;
- lease renewal before each candidate.

Claim contention with 10 claimers and 100 jobs: **0 duplicate claims** [M].

## 4. Automatic recovery

Already present [V]. The in-process sweeper:
- is guarded by a PostgreSQL advisory lock;
- has a bounded interval, a bounded batch size, and backs off after
  failures;
- recovers extraction and ingestion jobs and deletes dead sessions.

| Required test | Evidence |
|---|---|
| Killed worker | Live drill 5 |
| Expired lease | `TestStaleWorker`; drills 5 and 6 |
| Restart | `test_restart_does_not_lose_queued_or_in_flight_work` |
| Duplicate sweeper | `test_pass_is_skipped_while_another_instance_holds_the_lock` |
| Graceful shutdown | `test_stop_waits_for_the_pass_or_cancels_at_the_grace_deadline`, `test_stop_is_prompt_when_idle`, `test_lifespan_starts_and_stops_the_sweeper` |
| Retry exhaustion | `TestRetryExhaustion` |

**Live [M]:** a job held by a killed process completed **32.0 s** after
restart, against a 30 s lease. An interrupted ingestion recovered in
**32.1 s**.

## 5. LLM resilience

Already present [V] (`tests/unit/test_llm_resilience.py`,
`tests/integration/test_llm_job_resilience.py`):
- 429 with Retry-After, capped at 30 s;
- jittered exponential backoff;
- timeouts, network errors and 5xx treated as transient;
- 4xx treated as permanent;
- a provider cooldown after repeated 429s (60 s doubling, capped at 900 s);
- throttle deferral that does not consume attempts, up to 24 h;
- `test_no_llm_failure_writes_to_neo4j`.

New: the malformed-structured-output fixes in §2. Real-provider behaviour
was observed live on 2026-09-28:
- 429 `RESOURCE_EXHAUSTED` (daily free-tier quota of 20);
- 503 from Gemini;
- timeouts.

Each ended in deferral, then a terminal `llm_unavailable` after 3 attempts.
**No partial memory was written** [M].

## 6. Evaluation safety

Already present [V] (`tests/unit/test_eval_opt_in.py`,
`eval/opt_in.py`):
- Real-LLM evaluation requires `--real-llm` or `EVAL_REAL_LLM=1`. A real
  provider key in the environment is not enough.
- CI sets `LLM_PROVIDER=fake`.
- Real-LLM workflows are manual-only.

This pass's evaluation runs made **0 external calls**, and the agent runner
printed "skipping the REAL-LLM section".

## 7. Configuration

Already present [V] (`tests/unit/test_production_config.py`). In staging or
production, startup rejects:
- a short or missing `INTERNAL_API_TOKEN`;
- fake AI without `ALLOW_FAKE_AI`;
- missing database or Neo4j settings;
- DEBUG or echo modes;
- a relative storage directory;
- non-https origins;
- preview origins without a team scope.

It reports **all** problems at once.

The frontend production build refuses a missing or non-https
`BACKEND_ORIGIN` [V, 2026-09-27].

## 8. Health / readiness

Already present [V] (`test_readiness.py`, drills 1, 2 and 10):
- `/health` is liveness: `{status, version}`.
- `/health/ready` is readiness: 503 with status codes only, when PostgreSQL
  or Neo4j is unavailable or the migrations don't match.

The requested `/readiness` path is served as **`/health/ready`**. Render's
health check and the docs use that path. No alias was added [I].

## 9. Error handling

Already present [V] (`test_error_leaks.py`, 13 cases; `test_db_error_mapping.py`):
- 5xx responses carry no details, SQL, Cypher, paths or provider text.
- Live drills 1 and 2 checked for `SELECT`, `asyncpg`, `5432`, `bolt`,
  `7687`, `MATCH` and `Traceback` in responses: **none found** [M].
- Frontend error boundaries: `errorBoundary.test.tsx` [V].

## 10. Structured logging

Already present [V] (`test_observability.py`, `test_log_redaction.py`):
- JSON logs;
- a server-generated `request_id`, and a validated `X-Client-Request-ID`;
- request, user and job context carried into service, job and LLM log
  lines.

**Redaction:** tokens, passwords, authorization headers, search text and
document content are not logged. The Neo4j, SQLAlchemy and httpx loggers
are pinned to WARNING, since they leaked query parameters and URLs before
Phase 11.

The new repair-prompt log line logs field paths only [V].

## 11. Metrics

Already present [V]. `GET /internal/metrics` requires the
`X-Internal-Token` (401 without it). It reports:
- extraction and ingestion queue depth;
- the oldest pending job;
- abandoned jobs in the last 24 h;
- stuck documents;
- retry and error counts;
- requests by status class;
- errors by code;
- latency;
- LLM provider health and cooldown;
- sweeper state and recovery counts.

## 12. Security

| Area | Status |
|---|---|
| Sessions | Expired and idle sessions deleted by the sweeper [V]; tokens never logged [V]; cookie `HttpOnly` [V E2E], `SameSite=Lax` [V E2E], `Secure` in production [V unit]; CSRF Origin check on cookie writes [V]; rate limits per user and per email, global when per-IP is disabled (D5) [V]; trusted-proxy handling only through `TRUSTED_PROXY_IPS` [V] |
| Headers | HSTS (production builds), Permissions-Policy, X-Frame-Options DENY, Referrer-Policy, nosniff [V, `curl` of the production build] |

**Strict CSP (new).**
- *Before:* production `script-src` allowed `'unsafe-inline'` [I].
- *Now:* `frontend/proxy.ts` sets
  `script-src 'self' 'nonce-<per request>' 'strict-dynamic'`, with no
  `'unsafe-inline'`. `'unsafe-eval'` is included in development only.
- *Rendering:* pages render per request, because the root layout calls
  `connection()`.
- *Styles:* `style-src` keeps `'unsafe-inline'`. React renders `style`
  attributes, and styles cannot execute code (ADR 0029 amendment) [L].
- *Tests:* `e2e/csp.spec.ts` checks that:
  - the header is strict;
  - the nonce differs per request;
  - every served `<script>` carries the nonce;
  - six pages run with **zero** `securitypolicyviolation` events [V].

  `tests/unit/csp.test.ts` covers the policy string [V].

## 13. Upload protection

Already present [V] (`test_upload_limits.py`, 8 cases):

| Case | Result |
|---|---|
| Valid upload | Accepted |
| Exactly at the limit | Accepted |
| One byte over | 422 |
| Declared oversize | 413 before reading |
| Streamed oversize without a length | Cut off |
| Malformed | Clean error |
| Truncated multipart | No 5xx |
| 200 MiB | Peak allocation < 16 MB asserted |

**Filenames** never reach the filesystem:
- storage keys are generated server-side (`app/storage/local.py`);
- display names are sanitised: `../../etc/passwd` becomes `passwd`
  (`test_document_validation.py`) [V].

## 14. Migration safety

- **Head and check:** single head `320f6d898a92`; `alembic check` clean [V].
- **Index protection:** the HNSW index is excluded from autogenerate
  (`RAW_SQL_MANAGED_OBJECTS`, `test_migrations_static.py`) [V]. It was also
  present in the restored database in §15 [V].
- **Fresh and existing databases:** migrations pass on both
  (`test_migrations.py`, marker `scratch_db`) [V].
- **Policy:** forward-only. The destructive downgrades are documented as
  local-only, with a table (DEPLOYMENT.md §9) [I].

## 15. Backup / restore (local drill, performed now)

**Procedure:** DEPLOYMENT.md §6.
- Source: the test PostgreSQL database plus the local Neo4j.
- Target: an empty scratch database plus an empty scratch Neo4j on `:7688`.
- The backup was encrypted with a random passphrase.
- Both targets were **deleted afterwards**, along with the backup files and
  the passphrase.

| Step | Result |
|---|---|
| Encrypted backup (`backup.py create --encrypt`) | **5.52 s** [M] |
| Restore + `init_graph.py` | **12.33 s** [M] |
| Table counts | 17/17 tables match the manifest, including 3517 messages, 65 embeddings, 2 documents and chunks, and the alembic version [V] |
| Graph counts | Memory 107, Entity 145, Source 17; MENTIONS 196, DERIVED_FROM 42, RELATES_TO 89, SUPERSEDES 9, CONFLICTS_WITH 2, all match [V] |
| Temporal fields | `valid_from` 107, `valid_until` 9, not-active 11, all match [V] |
| Consistency | **7 orphans, identical before the backup and after the restore.** All 7 are Neo4j sources of the local dev account, whose PostgreSQL rows live in a *different* local database (the dev and test databases share one local Neo4j). A consistent pairing, as in staging, would show 0. Because of these, `verify` reports `ok: false`; the checker was not changed to hide it [V][L] |

A backend booted on the restored stores [V] returned:

| Check | Result |
|---|---|
| Readiness | 200 (`migrations: ok`) |
| Login with a restored account | 200 |
| Current memory | PaddleOCR `active`, no `valid_until` |
| Historical memory | EasyOCR `superseded`, `valid_until` 2026-05-01 |
| "Now" vs `as_of` March | Correct |
| Provenance | 1 source |
| Graph neighbourhood | `uses` relationships |
| Review queues | 1 memory, 1 entity |
| Document | `complete`, 1 chunk |
| Hybrid search | The vector channel returned restored memories after a fake-embedding backfill, so the HNSW index works |
| **User isolation** | User B reading user A's memory or document: **404** |

**Unverified until staging [L]:**
- the times at real data sizes;
- AuraDB and Neon restores;
- the scheduled `ops.yml` backup (no remote exists);
- a true RPO.

## 16. Performance

**Load**, M0 → after, p50 / p95 ms and rps. The "after" figures are from
the second post-change run:

| Endpoint | c=1 | c=10 | c=50 |
|---|---|---|---|
| memory search | 12.5→11.7 / 16.4→16.4 | 56.1→45.0 / 78.5→125.5 | 270.7→201.0, 174→235 rps |
| graph overview | 16.8→13.7 | 68.8→50.4 | 338.6→259.4 |
| neighbourhood (2 hops) | 16.7→14.0 | 65.5→45.8 | 357.4→230.5 |
| edge evidence | 13.5→11.8 | 59.1→41.0 | 298.9→214.8 |
| hybrid search | 23.4→19.9 | 106.3→76.1 | 521.6→349.8 |
| **agent turn** (offline LLM) | 23.8→15.2 | **250.8→81.4, 44→122 rps** | **1253.8→431.6, 40→111 rps** |

**Reading these numbers:**
- Code for every endpoint except agent turn is unchanged. Their 15–35%
  "improvement" is **machine noise**: the load average ranged from 4 to 17.
- The agent-turn change (~3× throughput) is well beyond that band. It
  repeated across both post-change runs. It is attributed to the §2 fix:
  the route now reads 10 rows instead of up to 1000 [M].
- Error rate: 0% in M0 and in the second post-change run.

**The first post-change run is discarded as invalid [M].** During it:
- the 5-minute load average hit **17.45**;
- a single request stalled for 39 s;
- `auth_me` at c=50 had a 0.24% error rate;
- endpoints the change never touches degraded 10–100×.

It was reported, not hidden, and rerun.

**Frontend.** M0 is the static build; "after" is the nonce CSP with dynamic
rendering.

| Measurement | M0 → after |
|---|---|
| JS bytes | Identical: critical `/memories` 168,462 B; `/login` 157,656 B |
| LCP `/memories` | 176 → 172 ms |
| LCP `/login` | 116 → 48 ms (the machine was quieter; noise) |
| Auth bootstrap | 159 → 165 ms |
| Canvas render at the budget | 610 → 624 ms |

No measurable cost from dynamic rendering at this scale [M].

**Graph end-to-end, after** (median ms):

| Size | Backend | Network | Layout | Paint |
|---|---|---|---|---|
| 99 elements | 122 | 9 | 130 | 39 |
| 299 elements | 132 | 5 | 646 | 45 |
| 597 elements | 99 | 10 | **2248** | 51 |

- **Layout is the bottleneck** (M0: 2224 ms at 597 elements).
- **Payloads stay within the Phase 8 budgets:** 150 nodes, 400 edges,
  2 hops.

**Jobs [M]:**
- 842 extraction jobs/min;
- ingestion 1265 documents/min;
- claim p95 41.8 ms.

**No optimisation was made for performance alone.** The only performance
change is a side effect of the §2 correctness fix.

## 17. CI

Present [I] (unchanged in this pass):

| Workflow | Contents |
|---|---|
| `ci.yml` | Backend unit tests, ruff, frontend typecheck, lint, unit tests and build, OpenAPI drift check, secret scan, pip-audit, npm audit |
| `integration.yml` | Full suite; `alembic heads` / `check`; deterministic evaluations with `LLM_PROVIDER=fake` |
| `e2e.yml` | Manual; picks up the new `csp.spec.ts` automatically |
| `ops.yml` | Backup, keep-alive |
| `real-llm-eval.yml` | Manual, opt-in |

**CI has never run on GitHub,** because no remote exists (not created, per
instructions) [L]. Every job's commands were run locally in this pass [V].

## 18. Failure drills

Numbering follows the M17 list in the request.

| # | Drill | How | Result |
|---|---|---|---|
| 1 | PostgreSQL unavailable | Live: TCP path cut | PASS. Live 200, ready 503, request 503 `database_unavailable`, no leak, healed [M] |
| 2 | Neo4j unavailable | Live | PASS. Ready 503, write 503 `graph_unavailable`, no leak, healed, then 201 [M] |
| 3 | LLM unavailable | Fault injection (transport errors, 5xx, timeout) | PASS [V] |
| 4 | LLM 429 | Fault injection (Retry-After, cooldown, deferral, 24 h cap, requeue) | PASS [V]; also observed live with real Gemini on 2026-09-28 [M] |
| 5 | Worker killed during extraction | Live: `kill -9` of the API and of the lease-holding process | PASS. Completed 32.0 s after restart [M] |
| 6 | Ingestion interrupted | Live | PASS. Recovered in 32.1 s [M] |
| 7 | Expired session | Live | PASS. 401 `authentication_required`; other session 200 [M] |
| 8 | Rate limit | Live | PASS. First 429 at request 21, Retry-After 50; other user 200 [M] |
| 9 | Migration mismatch | Live: database one revision behind | PASS. Ready 503 `migrations: mismatch` [M] |
| 10 | Missing document file | Integration test | PASS. FAILED `document_missing` [V] |
| 11 | Frontend/backend version mismatch | OpenAPI regeneration check (in sync), plus error-boundary tests | PASS [V] |
| 12 | Oversized upload | Integration tests (413 before read; 200 MiB bounded) | PASS [V] |

Command: `scripts/failure_drills.py`, **7/7 live**, run against the test
database only. Its internal drill numbers follow `docs/PHASE11_PLAN.md`
§25.

## 19. Remaining limitations

- **Environment:** all measurements are local, on a shared machine; there
  are no free-tier latency or cold-start figures.
- **Deployment checks never run:**
  - Vercel's upload path is unmeasured; through the local Next proxy, an
    over-limit body returns 500 instead of 413.
  - AuraDB runs Cypher 5 by default, while local development uses
    Cypher 25. It has not been run against AuraDB.
- **Runtime design limits:**
  - The provider cooldown is per process.
  - The in-process sweeper waits while Render sleeps.
  - The PDF timeout abandons the parse rather than cancelling the thread.
- **Per-IP rate limiting is off behind the proxy (D5).**
- **Content safety:**
  - `style-src 'unsafe-inline'` remains.
  - The agent prompt labels its data sections but does not fence them the
    way the extraction prompts do. Changing that prompt needs real-LLM
    evidence (ADR 0016), and the quota prevented collecting it in this pass.
- **Backup checks:** consistency shows 7 explained orphans locally, from the
  shared local Neo4j; restore times are for a small dataset.
- **Known unfixed issues:**
  - Pre-existing `ruff format` debt: 56 files at `a9da2db`; formatting is
    not a CI gate.
  - `eval/runner.py` has a pre-existing FK failure.
  - Disabled registration with an empty body returns 422, not 404.
- **Real coding-agent and real-LLM behaviour** remains untested in CI by
  design.

## 20. Staging blockers

Staging requires the owner to supply:
1. **Accounts:** Render, Neon, AuraDB Free and Vercel (not supplied).
2. **A private remote repository**, so CI, `ops.yml` backups and keep-alive
   can run (D10; not created, per instructions).
3. **Secrets:** `INTERNAL_API_TOKEN` (≥ 32 characters),
   `BACKUP_PASSPHRASE`, and the database and Neo4j credentials.
   - Real AI is optional: a `GEMINI_API_KEY` with enough quota. The current
     key allows 20 requests/day on `gemini-2.5-flash`.
4. **On the first deployment** (DEPLOYMENT.md §5):
   - measure the Vercel upload path;
   - run the Cypher 5 check;
   - run the staging subset of the drills;
   - run a restore drill from a real artifact.

Until these are done, the system is **not** deployed and **not**
production-ready.
