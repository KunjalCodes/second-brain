# Phase 11 — Production Hardening & Deployment Readiness

**Status:** PLAN, awaiting approval. Nothing in this document is
implemented.

**Checkpoint:** `a9da2db` (feat: add memory explorer ui). The working tree
was clean when this was written.

**Written:** 2026-09-26.

**Evidence labels.** Every factual claim carries one of these:

| Label | Meaning |
|---|---|
| **[V] verified** | Executed, a test pins it, or it was mechanically checked |
| **[I] inspected** | Code or docs were read; not executed |
| **[M] measured** | Numbers from an actual run, with the environment stated |
| **[P] provider-documented** | From current official provider docs, fetched 2026-09-26, source cited |
| **[A] assumption** | Not verified |
| **[L] limitation** | A known gap that is accepted or deferred |

File references are `path:line` at `a9da2db`.

---

## 1. Executive Summary

The system is **functionally complete** and runs correctly on a developer
laptop. It is **not ready to run unattended** anywhere else.

The audit found problems in four classes. The first two can lose or strand
data without anyone noticing, which matters more than any missing feature.

1. **Silent memory loss and stuck work in background jobs.**
   * If Neo4j is unavailable while an extraction candidate is being
     applied, the candidate is recorded as `INVALID` and the job completes.
     The extracted memory is dropped permanently, with no retry. [I]
   * The document-ingestion sweep exists but is never called. A process
     that dies mid-parse leaves the document stuck in `parsing` forever.
     [V: grep finds no caller]
   * A retry after a crash at a specific point can never succeed, because
     a foreign key blocks it.
   * Jobs whose retries are exhausted never reach a terminal state.
   * A slow job can outlive its lease, and the stale worker can then
     overwrite the new owner's result.
2. **Configuration and exposure.**
   * Production startup accepts fake LLM and embeddings, a missing internal
     token, a relative local storage directory, and `DEBUG` / SQL echo.
   * `/health/ready` and two error paths can return raw driver or SDK
     exception text to clients.
   * Uploads are read fully into memory before the size check.
   * Expired sessions are never cleaned up.
3. **No operational visibility.**
   * Logs are plain text and not correlated by request.
   * The access log records search text from query strings.
   * There are no metrics.
   * Nothing schedules the recovery sweeps, and the script the docstring
     points to (`scripts/run_sweep.py`) does not exist.
4. **Durability not designed.**
   * No backup or restore procedure exists for PostgreSQL or Neo4j.
   * The free tiers give only a 6-hour Neon restore window and manual
     Aura snapshots.
   * Autogenerate proposes dropping the HNSW index on every new migration.
   * Every downgrade is destructive.

**Recommendation.** Phase 11 fixes classes 1–4 **in code, with tests and
failure drills, locally first**. The first deployment target is a
**private staging environment**: invite-only accounts created with the
operator CLI, and registration off. A **public demo** is a later, separate
decision (section 3).

No new AI features. No Redis or Celery, because nothing measured requires
them. The existing PostgreSQL queue is kept and fixed.

Only a few small dependencies may be proposed, each gated by a decision
in section 29.

---

## 2. Current System State

What exists at `a9da2db`, taken from the repository and the Phase 10
report:

| Area | State | Evidence |
|---|---|---|
| Backend tests | 1270 passed (real Neo4j + PostgreSQL) | [M] Phase 10 §29.5 |
| Frontend tests | 63 unit/component, 29 E2E (local production build) | [M] Phase 10 §29.5 |
| Graph conformance | 39/39 | [M] |
| Retrieval evaluation | Byte-identical to `873a409` | [M] |
| Deployment artifacts | `backend/Procfile` only. No `render.yaml`, `vercel.json` or Dockerfile | [V] |
| Migrations | 7 revisions, head `320f6d898a92`, run manually | [I] `backend/migrations/versions` |
| Neo4j schema | Applied manually by `scripts/init_graph.py`, not at startup | [I] |
| Background work | In-process `BackgroundTasks` ticks + PostgreSQL queue tables. No separate worker, no scheduler | [I] `routes/conversations.py:153-172`, `routes/documents.py:54-146` |
| LLM providers | Gemini + Fake implemented. `openai` / `anthropic` pass validation but the factory raises | [I] `app/llm/factory.py:33-38` |
| Storage | `DocumentStorage` protocol; only `local` allowed | [I] `app/storage/base.py:23-54`, `config.py:218-226` |
| Observability | Plain-text logs, request id header, no metrics | [I] `app/core/logging.py:13-27` |
| CI | Ruff, unit tests (py3.12/3.14), isolation audits, frontend job. Integration tests are manual/weekly | [I] `.github/workflows/*.yml` |
| Remote repository | None. CI has never run on GitHub | [V] `git remote -v` is empty |

Known open items carried from earlier phases:

* G1: no document → derived memories endpoint. [L]
* G3: registration-status endpoint. [L]
* G4: client IP behind the proxy. [L]
* The pre-existing `eval/runner.py` foreign-key cleanup failure. [V]

---

## 3. Deployment Target

### 3.1 Three environments, never conflated

| | Local development | Free-tier demo / staging | Real production |
|---|---|---|---|
| **Purpose** | Build and test | Show the system; controlled use by the owner and invited users | Real users relying on their data |
| **Availability** | None | Best effort. Cold starts; the provider may suspend | SLO-backed, always on |
| **Data durability** | Developer's disk | **Weak.** Provider limits (§7); our own logical backups | Managed backups + PITR + tested restores |
| **Auth** | `dev_header` or session | Session; registration **off**; operator-created accounts | Session + account recovery + abuse controls |
| **LLM** | Fake (default) or free key | Fake or a free key; quota exhaustion is expected | Paid tier with quota headroom |
| **Cost** | 0 | 0 | Not zero, and out of scope |
| **Called** | "local" | "staging" or "demo". **Never "production"** | "production" (Phase 11 does not build it) |

### 3.2 Initial target: private staging

**Recommendation: private staging first.** The repository supports this
choice on its merits:

* The only account path suitable for a demo is the operator CLI
  (`scripts/manage_users.py`). Public self-registration would expose
  quota-limited LLM calls (Gemini free keys hit 429 repeatedly in Phases
  7 and 10 [M]) and storage to strangers. There is no abuse control
  beyond rate limits, and no email verification.
* Per-IP rate limiting collapses into one shared bucket behind the
  proxy (§14). Public traffic would lock everyone out of login together.
* **Vercel Hobby is non-commercial only** [P: vercel.com/docs/limits/fair-use-guidelines].
  A personal portfolio fits; anything commercial does not.

A **public demo** means read-mostly access to a seeded demo account, or
self-registration. It becomes reasonable only after all of these:

* §14 per-IP attribution is solved;
* the LLM is fake, or protected by a strict global budget;
* the upload limit fits the platform (§16).

This is decision **D1**.

---

## 4. Current Architecture Audit

These are the findings the rest of the plan acts on. Each maps to a
milestone in §27.

### 4.1 Backend

* **Startup degrades instead of failing fast.** If Neo4j or PostgreSQL
  can't be reached, the error is logged and the process keeps serving
  (`main.py:80-88, 99-108`). This is intentional, to avoid restart loops.
  Readiness reports the failure. [I]
* **Production validators exist** for auth mode, the Secure cookie,
  origins, scrypt cost and provider names (`config.py:341-390`). **They
  do not reject:** [V: read `config.py:341-392`]
  * `LLM_PROVIDER=fake` or `EMBEDDING_PROVIDER=fake`. The comment at
    `db/engine.py:83-84` claims such a guard exists. It does not.
  * A missing or weak `INTERNAL_API_TOKEN`. The endpoints fail closed,
    but with a 500 whose message names the variable (`deps.py:359-362`).
  * A relative, ephemeral `DOCUMENT_STORAGE_DIR`.
  * `DEBUG`, `DATABASE_ECHO` (logs SQL), `LOG_LEVEL=DEBUG`.
  * An `http://` `FRONTEND_URL`.
  * Missing `NEO4J_*` or `DATABASE_URL`. These are only logged.
* **Shutdown** closes the drivers, but in-process background ticks are not
  drained. The Procfile has no graceful-shutdown timeout. [I]
* **Stale metadata:** the FastAPI description says "no authentication",
  the version is 0.2.0, and `/health` reports `"phase": 2`
  (`main.py:125-131`). [I]
* **Dead configuration:** `REDIS_URL`, `STORAGE_*` and `GROQ_API_KEY` are
  in `.env.example` but unused. `groq` is not in `_KNOWN_PROVIDERS`. [I]

### 4.2 Frontend

* **No error boundaries.** There is no `error.tsx`, `global-error.tsx` or
  `not-found.tsx`. [V: `ls`]
* **`BACKEND_ORIGIN` silently falls back** to `http://localhost:8000` at
  build time (`next.config.ts:9`). [I] A production build without it
  would ship a broken proxy.
* **Missing headers:** no HSTS, no Permissions-Policy. [I]

### 4.3 Operations

* **No sweep scheduler, and the referenced sweep script is missing.** No
  cron jobs, keep-alive or backups exist. [V]
* **Procfile** (`web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`)
  has no proxy-header, graceful-shutdown or access-log flags. [V]

---

## 5. Deployment Topology

```text
Browser
  │  HTTPS, same origin, HttpOnly sb_session cookie
  ▼
Next.js (Vercel Hobby)   ── /api/* rewrite (fixed at build time) ──┐
                                                                   ▼
                                    FastAPI (Render Free web service, 1 instance)
                                      │  in-process request handling
                                      │  in-process periodic sweeper   (NEW, §6)
                                      ├──► PostgreSQL (Neon Free) + pgvector
                                      ├──► Neo4j (AuraDB Free)
                                      ├──► document bytes: transient local disk (§16)
                                      └──► LLM (fake | Gemini free key)
External, optional: GitHub Actions cron → keep-alive + backups (needs a remote repo, D10)
```

### 5.1 Is the free-tier stack still compatible? (re-verified 2026-09-26)

| Component | Relevant current fact | Source | Compatible? |
|---|---|---|---|
| Vercel Hobby | Non-commercial only | [P] vercel.com/docs/limits/fair-use-guidelines | Yes, for personal staging |
| | External rewrite timeout 120 s | [P] vercel.com/docs/limits | Yes. The agent turn is ≤60 s server-side (`agent_turn_timeout_seconds`) [I] |
| | Function body limit 4.5 MB (413 above) | [P] vercel.com/docs/functions/limitations | **Unknown** for pure external rewrites. The limit for proxied rewrites is not stated [P: unverified]. **20 MB uploads may fail.** Must be measured in M12 (§16) |
| | 100 GB transfer, 1M edge requests / month | [P] vercel.com/docs/plans/hobby | Yes |
| Render Free | Spins down after 15 min idle; ~1 min cold start | [P] render.com/docs/free | Yes. The UI shows "Waking the server…" [V Phase 10] |
| | 750 instance-hours / month per workspace | [P] | Yes for one service |
| | **Ephemeral filesystem, no persistent disk** | [P] | Only if document bytes are treated as transient (§16) |
| | **No free cron jobs** ($1/month minimum) | [P] render.com/docs/cronjobs | Needs the in-process sweeper and/or GitHub Actions cron |
| | Free Postgres expires after 30 days, no backups | [P] | **Do not use.** Use Neon |
| Neon Free | 0.5 GB / project; 100 CU-h / month; scale-to-zero after 5 min | [P] neon.com/pricing, neon.com/docs/introduction/plans | Yes. The first query after idle pays wake latency |
| | pgvector on every plan | [P] neon.com/docs/extensions/pgvector | Yes. HNSW availability depends on the pgvector version [A]; verify in M12 |
| | 6 h restore window | [P] | Weak backup (§7) |
| AuraDB Free | 200k nodes / 400k relationships | [P] Aura FAQ | Yes for staging scale |
| | Paused after 72 h inactivity; **deleted after 30 days paused** | [P] neo4j.com/docs/aura/managing-instances/instance-actions/ | Needs keep-alive + our own exports |
| | What counts as "activity" | Not defined on the fetched pages [P: unverified] | Assume writes are needed [A] |
| | Manual on-demand snapshots only | [P] …/backup-restore-export/ | Weak backup (§7) |
| | Cypher 5 default | [P] …/cypher-version/ | Compatibility of `COLLECT {}` / `CALL () {}` must be run against Aura in M12 [A] |

**Alternatives recorded, not chosen:**

* Koyeb Free: one 512 MB / 0.1 vCPU instance, scale-to-zero after 1 h
  idle, no volumes [P koyeb.com/docs/reference/instances]. 0.1 vCPU is
  likely too slow for scrypt logins at production cost (§15) [A].
* Fly.io: no free allowance, trial only [P].
* Railway: $5 trial, then $1/month credit [P].

---

## 6. Background Job Reliability

### 6.1 How it works today [I]

**Extraction**

* **Enqueue.** A `pending` job is created in the same transaction as the
  message, so it is idempotent: `ON CONFLICT (target_message_id,
  extractor_version)` (`extraction_repository.py:101-150`).
* **Claim.** `SELECT … FOR UPDATE SKIP LOCKED` in a CTE, setting
  `in_progress`, `locked_at`, `locked_by` and `attempts+1`
  (`extraction_queue.py:45-101`).
* **Retries.** Max 3 attempts. Backoff 60 s·2^(n-1), capped at 1 h
  (`runner.py:104-140`).
* **Lease.** 600 s. Expired leases are reclaimed only inside
  `run_sweep_tick` (`extraction_queue.py:103-150`, `runner.py:1174-1239`).
* **Sweep endpoint.** `POST /internal/extraction/sweep`, protected by
  `X-Internal-Token` (`routes/extraction.py:66-89`). **Nothing calls it.**
* **Idempotency.**
  * Layer 1: job uniqueness.
  * Layer 2: per-job `candidate_hash` uniqueness
    (`extraction_repository.py:272-353`).
  * Each Neo4j memory write is a single transaction.
  * A crash between the Neo4j commit and the PostgreSQL commit can
    duplicate at most one candidate. This is documented as accepted
    (`docs/PHASE2_HARDENING.md` §2.2).

**Ingestion.** Same queue pattern (`ingestion_queue.py`), with one job per
document. The stages are validating → parsing (60 s timeout) → chunking →
per-chunk extraction jobs → finalize (`documents/ingestion.py`).

### 6.2 Defects found

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| **J1** | `_apply_candidate` catches **every** `AppError` and records the candidate as `INVALID`. That includes `GraphUnavailableError` / `DatabaseUnavailableError` (both `AppError` subclasses, `core/errors.py:62,76`) | [V] `extraction/runner.py:691-699` read; [I] the job is then marked done (`:437-456`) | A Neo4j outage mid-job **permanently drops extracted memories**, with no retry. Worst finding in the audit |
| **J2** | `run_ingestion_sweep_tick` has no caller | [V] grep across `app/`, `scripts/`, `tests/` | A crash mid-ingestion leaves the document in `parsing` / `validating` forever. A failed-but-retryable ingestion is retried only if someone else uploads |
| **J3** | No scheduler for the extraction sweep; `scripts/run_sweep.py` is missing | [V] | Expired leases and backoff-delayed retries are never processed unless new traffic triggers a live tick. The live tick does not reclaim leases |
| **J4** | `mark_done` / `mark_failed` filter only on `id`, not `locked_by`. The lease is never renewed, and a sweep batch of up to 50 jobs shares one `locked_at` | [V] `extraction_queue.py:160-216`; [I] `runner.py:1169-1170` | A slow batch (LLM retries ≈127 s per call, plus per-candidate embedding calls) can pass 600 s. A second worker then reclaims the job, and the stale worker can overwrite its result |
| **J5** | Exhausted `failed` jobs never become `abandoned`. Message `extraction_status` never becomes FAILED | [I] `runner.py:443`; `extraction_queue.py` | No terminal state, so no alertable signal. The UI shows "queued" forever |
| **J6** | Chunk rows and their extraction jobs commit before the ingestion job is marked done. `extraction_jobs.target_chunk_id` has an FK with no `ON DELETE` | [V] `db/models.py:219-221`; [I] `ingestion.py:228-262` | After a crash at that point, every retry's `delete_all_for_document` violates the FK. The document can never recover |
| **J7** | Chunks never move to QUEUED / IN_PROGRESS. A chunk whose job is abandoned stays PENDING, so the document never finalizes | [I] `runner.py:222-232, 461-494` | Documents stuck in `extracting` |
| **J8** | Parse timeout: `wait_for(to_thread(...))` cannot stop the thread | [I] `ingestion.py:186-195` | A runaway PDF parse keeps a CPU busy after the "timeout". With 0.1–0.5 vCPU on a free tier, one bad PDF degrades the whole service |
| **J9** | Parse timeout marks the document FAILED but the job retryable | [I] `ingestion.py:116-133` | Inconsistent state shown to the user |
| **J10** | `/agent-turn` schedules the extraction tick without the explicit commit that `/messages` does | [I] `routes/agent.py:115-122` vs `routes/conversations.py:153-172` | The tick can miss the job, and it waits for the (absent) sweep |
| **J11** | The embedding backfill scans only the oldest 200 memories per user, with no cursor. `list_ids` has no offset | [I] `semantic/embedding_pipeline.py:128`, `user_repository.py:56` | Memories past 200 per user are never backfilled after an embedding outage |

### 6.3 Is the PostgreSQL queue enough? (hypothesis, measured in M0)

The queue design is sound: `SKIP LOCKED` claim, lease, attempts, backoff
and idempotency layers. The defects above are **logic and wiring bugs, not
capacity problems**. Expected load for staging is a handful of users.

**Decision rule.** Keep the PostgreSQL queue unless M0 measures either:

* claim contention (claim p95 > 50 ms at 10 concurrent workers), or
* queue throughput below the LLM's own rate limit.

Redis / Celery would add a paid or extra service and fix none of J1–J11.

### 6.4 Planned fixes (M1, M2)

1. **J1: classify errors.** Infrastructure errors (`GraphUnavailableError`,
   `DatabaseUnavailableError`, `DatabaseError`, `GraphQueryError`) must
   propagate out of `_apply_candidate`. The job is marked `failed`
   (retryable) and the candidates not yet applied are retried under
   layer 2. Only domain validation (`ValidationError`, `NotFoundError`
   from the domain) becomes INVALID.
   * Test: a fake graph client that raises `GraphUnavailableError` on
     candidate 2 of 3. Expected: candidate 1 applied; job `failed`;
     retry applies 2 and 3; no INVALID rows.
2. **J2 / J3: in-process periodic sweeper** (a lifespan asyncio task).
   Every `SWEEP_INTERVAL_SECONDS` (default 60) it runs the extraction
   sweep and the ingestion sweep. A PostgreSQL **advisory lock** means
   only one instance sweeps at a time. It stops on shutdown.
   * It works whenever the process is awake. On Render, pending work is
     picked up within one interval of wake-up.
   * The internal HTTP endpoints stay, for an external trigger.
   * Add `scripts/run_sweep.py`, the missing script the docstring
     references, as a thin CLI over the same functions.
3. **J4: lease ownership.**
   * `mark_done` / `mark_failed` / `reclaim` add `WHERE locked_by = :me`;
     a lost lease makes them no-ops and logs it.
   * Renew the lease (`locked_at = now()` for that owner) before each job
     and after each LLM call.
   * The sweep processes jobs from a claimed batch only while the lease is
     valid.
   * Test: two workers, lease expiry mid-job; the stale worker's
     `mark_done` affects 0 rows.
4. **J5 / J7: terminal states.**
   * Exhausted attempts → `abandoned` with its reason. The message
     becomes FAILED and the chunk FAILED, so the document finalizes to
     `PARTIALLY_COMPLETE` / `FAILED`.
   * An operator CLI `scripts/requeue_jobs.py` requeues abandoned jobs
     (by reason or age), for recovery after a quota outage (§12).
5. **J6: resumable chunking.** No migration needed:
   * If chunks already exist for the document **and** extraction jobs
     reference them, skip re-chunking and resume at "ensure one
     extraction job per chunk" (idempotent via the existing unique key).
   * Only delete and rewrite chunks when no job references them.
   * Test: crash injected after the jobs commit; the retry reaches
     `EXTRACTING` / `COMPLETE`.
6. **J8 / J9: bound the damage** without a subprocess pool:
   * a process-wide `asyncio.Semaphore(1)` around parsing, so at most one
     parse thread runs;
   * parse timeout → document FAILED **and** job abandoned (consistent);
   * the thread-cannot-be-killed limitation is documented [L].
   * A subprocess parser is deferred (§28).
7. **J10:** commit before scheduling the tick in `/agent-turn`, matching
   `/messages`.
8. **J11:** cursor-based backfill (`WHERE m.id > $after ORDER BY m.id`)
   and user pagination. Test with >200 memories.

### 6.5 Failure behaviour: today → after Phase 11

| Scenario | Today [I] | After Phase 11 (drill in §25) |
|---|---|---|
| Crash during extraction | `in_progress` until someone calls the sweep, which nobody does | Sweeper reclaims within lease + interval. At most one duplicate candidate (documented) |
| Process restart | Pending jobs wait for new traffic | Picked up by the sweeper after boot |
| Worker dies | Same as crash | Same as crash |
| DB connection drop | `pool_pre_ping`; the job may wait for the lease | Unchanged mechanics, now recovered by the sweeper |
| LLM timeout / 429 | 4 attempts in-adapter, then 3 job attempts, then stuck `failed` | Retry-After / cooldown (§12), then `abandoned` with reason; requeue CLI |
| Invalid structured output | One repair call, then `abandoned` | Unchanged (correct) |
| Parse failure | Document FAILED, job abandoned | Unchanged |
| Parse timeout | Document FAILED, job retryable (inconsistent) | Both terminal |
| **Neo4j unavailable** | **Candidates INVALID, job DONE: data loss** | Job `failed`, retried; nothing lost |
| PostgreSQL unavailable | 503 to requests; jobs wait | Same; readiness 503; sweeper backs off |

### 6.6 Agent turn [I]

* The turn runs under a 60 s timeout, with a budget of 6 tool calls and
  a 15 s timeout per LLM call (`agent_service.py:177-194, 329`).
* On `LLMUnavailableError` the request-scoped transaction rolls back. The
  user's message is **not** stored, and the client gets 503. **Keep this
  atomic behaviour:** the frontend keeps the typed question on error
  [I `AskMemory.tsx`]. It is honest, and nothing is half-written.
* The DB session stays open for the whole turn, up to 60 s. That is
  connection-pool pressure, since the pool is 5 + 5 overflow
  (`config.py:84-85`). **Measure in M10.** If it matters, release the
  session during LLM calls.

---

## 7. Database Durability

### 7.1 What can and cannot be reconstructed

| Data | Store | Reconstructable? |
|---|---|---|
| Memories, entities, relationships, validity, status, merges | Neo4j | **No.** Canonical; must be backed up |
| Users, accounts, conversations, messages | PostgreSQL | **No** |
| Review candidates + audit log | PostgreSQL | **No.** Governance history |
| Documents metadata + chunks (provenance text) | PostgreSQL | Only by re-ingesting raw bytes, which are not durable (§16). **Treat as not reconstructable** |
| Extraction jobs / candidates | PostgreSQL | Partially. History only; not needed to serve |
| Memory embeddings | PostgreSQL (pgvector) | **Yes**, from Neo4j via the backfill (after J11) |
| Sessions, rate-limit counters | PostgreSQL | Disposable. Users log in again |
| Evaluation runs | PostgreSQL | Re-runnable (deterministic ones) |

**Cross-store consistency.** PostgreSQL and Neo4j backups are taken at
different moments, so a restore can leave references between them
(embeddings ↔ memories, provenance ↔ sources, candidates ↔ memories) out of
step. A read-only `scripts/check_consistency.py` reports orphans in both
directions after any restore.

### 7.2 Strategy per environment

| | PostgreSQL | Neo4j | Frequency / retention |
|---|---|---|---|
| **Local** | `pg_dump -Fc` via `scripts/backup.sh` | `scripts/export_graph.py` (user-scoped Cypher reads to JSONL; no APOC dependency, since APOC availability on Aura Free is unverified [A]) | On demand |
| **Staging (free)** | Neon's 6 h restore window [P] **plus** a nightly `pg_dump` from GitHub Actions to an encrypted artifact (needs D10) or run manually by the operator | Manual Aura snapshot [P] **plus** the nightly `export_graph.py` | Nightly; keep 7 dumps. GitHub artifact retention is a setting; 500 MB free storage for private repos [P] |
| **Production** | Managed PITR + daily snapshots, 30 days | Managed scheduled backups | Out of scope |

### 7.3 Restore test (required before a backup counts)

1. Create an empty local PostgreSQL database and an empty local Neo4j.
2. `pg_restore`, then `alembic current`, which must equal head.
3. `import_graph.py`, then `init_graph.py` (constraints).
4. Run `check_consistency.py`, which must report zero orphans. Compare
   row and node counts against the numbers recorded at backup time.
5. Boot the API against the restored stores. `/health/ready` returns 200.
   Run the Phase 8 conformance subset and a login with a restored account.
6. Record the elapsed time as the **measured RTO**. Record the data loss
   window as the RPO: the backup interval.

This runs locally in M9 and on staging data in M12.

### 7.4 Explicitly **no** guarantee on the free tier [P/L]

* Neon keeps only 6 h of history.
* Aura Free has no scheduled backups, and an instance is deleted 30 days
  after pausing.
* Render's disk is wiped on every restart.
* A lost nightly job means up to 24 h of loss, or more if nobody notices.
* There is no off-site copy unless the operator downloads the artifacts.

---

## 8. Migration Safety

### 8.1 Findings

* Seven linear revisions; head `320f6d898a92`. [I]
* **Every downgrade drops tables or columns.** The `1dc0dd91aafc`
  downgrade fails if `LIKELY_DUPLICATE` / `FLAGGED` rows exist. [I]
* **HNSW index trap.** The index is created with raw SQL in `6202eeca7d8d`
  and is not on the ORM model. `migrations/env.py` has **no
  `include_object`**. [V: grep] Every autogenerate therefore proposes
  `DROP INDEX memory_embeddings_hnsw_cosine`, and it has been removed by
  hand five times (docstrings of revisions 3–7; `docs/PHASE9_PLAN.md:991-994`).
* Migrations are not run at startup (correct). There is no migration test
  of any kind. [I]
* **Locking risk.** Future `CREATE INDEX` on large tables should be
  `CONCURRENTLY` (outside a transaction). At staging scale this is not a
  concern today [A].

### 8.2 Planned (M7)

1. **`include_object` in `env.py`.** Ignore objects in an explicit
   allow-list of raw-SQL-managed objects (starting with
   `memory_embeddings_hnsw_cosine`). The list lives in one constant, with
   a comment.
2. **Migration tests** (integration, real PostgreSQL):
   * `alembic heads` has exactly one head;
   * upgrade from empty to head, then `alembic check` reports "no new
     upgrade operations". This detects model/migration drift **and**
     proves the HNSW index survives autogenerate;
   * after upgrade, `pg_indexes` contains the HNSW index.
3. **Policy, documented in `docs/API.md` §Apply the schema:**
   * **forward-only in staging and production**; downgrades are for local
     use only and are marked destructive in their docstrings;
   * migrations run as an explicit deploy step before the new code
     starts serving (§24), never on app startup;
   * any destructive change needs expand → migrate → contract across two
     deploys.
4. **Readiness checks the migration state** (§10): the DB revision must
   equal the code's expected head.

---

## 9. Observability

Zero-cost and in-process. No paid service and no new dependency, except
where D6 approves one.

### 9.1 Logging (M5)

* **JSON lines** from a stdlib `logging.Formatter` subclass, selected by
  `LOG_FORMAT=json|text` (json in staging). Fields:
  * `ts`, `level`, `logger`, `msg`;
  * `request_id`, `user_id`, `route`, `method`, `status`, `latency_ms`;
  * `job_id`, `job_kind`, `attempt`, `state`;
  * `llm_provider`, `llm_model`, `llm_latency_ms`, `retry_count`,
    `error_code`.
* **Correlation.** `contextvars` set by the request middleware and by job
  runners. A logging filter injects them into every record.
* **Request id.**
  * Accept a client `X-Request-ID` only if it matches
    `^[A-Za-z0-9._-]{1,64}$`; otherwise generate one. This closes the log
    injection path at `main.py:169-172` [I].
* **One access line per request** from our middleware: route **template**
  (e.g. `/memories/{id}`), never the raw path or query string.
  * Disable uvicorn's access log (`--no-access-log`). Today it records
    `?q=` search text [I].
* **Never logged** (enforced by a unit test that captures logs during
  auth and search flows and asserts absence):
  * passwords;
  * `sbs_` tokens or cookie values;
  * `Authorization` headers;
  * API keys;
  * request or response bodies;
  * memory or document content;
  * email addresses (auth already logs `user_id` only [I]).
* **Error texts** from Gemini, Neo4j and SQLAlchemy are logged at WARNING
  with the exception class and a truncated message (≤300 characters), and
  are never returned to clients (§11).

### 9.2 Metrics (M5)

`GET /internal/metrics` (X-Internal-Token), JSON. Two sources:

* **In-process counters** since start:
  * requests by route template × status class;
  * 429 count by limit class;
  * auth failures by code;
  * LLM calls, errors by class (429 / timeout / transport / invalid) and a
    latency histogram;
  * graph, retrieval and agent latency histograms (fixed buckets);
  * sweeper runs and last-run time.
* **Gauges computed on request from PostgreSQL:**
  * extraction and ingestion queue depth by status;
  * age of the oldest `pending` job;
  * count of `abandoned` jobs in the last 24 h;
  * documents stuck in a non-terminal state for more than 30 min;
  * active sessions.

Counters reset on restart, which is accepted for staging [L]. The
Prometheus text format is an option under D6 (`prometheus_client`).

**Alerting (staging):** none automated. The operator runbook checks
`/internal/metrics`. An optional GitHub Actions cron fails a workflow run,
which emails the owner, when the oldest pending job is older than 1 h or
readiness is 503.

---

## 10. Health & Readiness

| | Liveness `/health` | Readiness `/health/ready` |
|---|---|---|
| Question | Is the process running? | Can this instance serve normal requests now? |
| Checks | None external. Returns 200 if the event loop answers | PostgreSQL `SELECT 1` (with reconnect); Neo4j `verify_connectivity` **with reconnect** (missing today [I]); **Alembic revision == expected head** (cached 60 s); required configuration present |
| Must not check | Databases | The LLM (a quota outage must not take the API offline; jobs retry later) [I, `health.py:14-16`] |
| Body | `{status, version}`. Drop the stale `phase` | `{status, checks: {postgres, neo4j, migrations}: "ok" \| "unavailable" \| "mismatch"}`. **Codes only**, no exception text (today `health.py:53,65` returns `exc.message`) [V] |
| Status codes | 200 | 200 / 503 |

The frontend needs no health endpoint of its own: Vercel serves the UI.
Render's health-check setting points at the backend's `/health/ready`.

---

## 11. Error Handling

**Current state [I]:**

* The consistent envelope `{"error":{code,message,request_id,details?}}`
  covers `AppError`, request validation (field path + message only, no
  input echo) and unhandled exceptions (opaque 500).
* Database errors are mapped without SQL.
* Stack traces are never returned.

**Leaks to close (M4):**

| Where | Leak | Fix |
|---|---|---|
| `health.py:53,65` | Driver exception text on an unauthenticated endpoint | Codes only (§10) |
| `graph/connection.py:63` | Raw Neo4j driver exception inside a `ConfigurationError` message | Generic message; details go to the log |
| `llm/providers/gemini.py:224-233` | SDK exception text, and `GEMINI_API_KEY` named, reaching agent clients as 503/500 | Generic `llm_unavailable` / `llm_misconfigured`; details go to the log |
| `api/deps.py:360` | "INTERNAL_API_TOKEN not set" | Startup validation makes this impossible in staging; generic message otherwise |
| Framework `HTTPException` (404/405, `routes/auth.py:101`) | `{"detail":…}` shape instead of the envelope | Register a Starlette `HTTPException` handler that emits the envelope. The frontend accepts both shapes [I `client.ts:93`] |

**Test:** a parametrized test forces each error class and asserts:

* the status code;
* the envelope shape;
* the body does not contain `Traceback`, `SELECT`, `MATCH (`, `/Users/`,
  `bolt://`, `postgresql`, or `key`.

---

## 12. LLM Reliability

### 12.1 Current behaviour [I]

* Gemini adapter: 4 attempts at 1 / 2 / 4 s backoff, no jitter
  (`gemini.py:185-216`).
* Error mapping:
  * 429 and 5xx → `LLMUnavailableError`;
  * 401 / 403 / other 4xx → `ConfigurationError`;
  * timeout → `LLMUnavailableError`.
* Structured output is validated with `model_validate_json`; a failure
  raises `LLMResponseInvalidError`. The runner makes one repair call, then
  abandons the job.
* **Gaps:**
  * `Retry-After` is ignored.
  * There is no cooldown or circuit breaker.
  * Transport errors (httpx connect/read) bypass both the retry loop and
    the mapping. Only `TimeoutError` and `APIError` are caught
    (`gemini.py:185-233`).
  * The retry loop itself is untested (`tests/unit/test_llm_gemini.py`
    covers only the mapper).
* The embeddings adapter has the same shape and the same gaps
  (`embeddings/providers/gemini.py:96-155`).

### 12.2 Design (M3)

| Situation | Behaviour |
|---|---|
| 429 with `Retry-After` / `retryDelay` | Wait `min(value, 30 s)` if within the call's deadline; otherwise stop retrying and raise `LLMRateLimitedError(retry_after)` |
| 429 without a hint | Exponential backoff with full jitter (base 1 s, cap 8 s), max 3 retries, within the per-call deadline |
| Timeout / 5xx / transport error | Same retry policy; transport errors mapped to `LLMUnavailableError` (fixes the gap) |
| Repeated 429 (quota) | **Provider cooldown:** after 3 consecutive 429s, the process-wide state for that provider skips calls for `max(retry_after, 60 s)`, doubling up to 15 min. It is in-memory, per process (§28 explains why that is enough) |
| Malformed / schema-invalid output | Unchanged: one repair call, then `abandoned` with `llm_response_invalid`. Not retried, because it is deterministic enough to waste quota |
| Retry exhaustion inside a job | Job `failed` with `next_attempt_at = max(backoff, cooldown_end)`. **429 and cooldown failures do not consume job attempts**, up to a max age of 24 h. Then `abandoned` with `llm_unavailable` |
| Quota exhaustion for the day | Jobs wait in `failed` with the cooldown; nothing is written to Neo4j; the canonical memory is untouched. After 24 h: `abandoned`; operator runs `requeue_jobs.py --reason llm_unavailable` |
| User-visible (agent turn) | 503 `llm_unavailable` with `Retry-After` when known. The UI shows "The AI provider is busy; try again in N s". The question text is kept |
| User-visible (extraction) | Message stays `queued` while retrying; `failed` after abandonment (J5). The UI shows it in memory/document status |

**Invariant:** no LLM failure path writes to Neo4j. The only Neo4j writes
happen after a validated candidate passes the deterministic decision
engine (ADR 0007). A test asserts that zero Neo4j writes happen when the
fake LLM raises every error class.

**Tests:** a fake transport injects 429 (with and without `Retry-After`),
timeout, httpx `ConnectError` and 500. Assert attempt counts, waits (using
a fake clock) and cooldown engagement and release. FakeLLM stays the
default everywhere.

---

## 13. Provider Resilience

* **The interface** (`llm/base.py:22-62`) is provider-neutral:
  `complete_structured(prompt, schema, …) -> LLMResult[T]`, with the
  validated value plus provider, model, tokens, latency and raw output.
  Adding OpenAI, Anthropic, Groq or a local model means one adapter each,
  with no change to extraction, the decision engine or storage. [I]
* **Today:**
  * `openai` / `anthropic` pass validation and then fail at the factory.
  * `groq` has a key setting but is not a known provider.
* **Phase 11 fix, validation only:** production startup rejects a
  configured provider that has no adapter, so the failure is at boot
  rather than on the first job.
* **No new providers** are implemented. A second provider does not make
  staging more reliable while the first is free-tier. Multi-provider
  failover is deferred (§28).
* **Error contract every adapter must follow** (so failures cannot
  corrupt memory):
  * `LLMUnavailableError` (retryable);
  * `LLMRateLimitedError` (with retry-after);
  * `LLMResponseInvalidError` (not retryable);
  * `ConfigurationError` (fatal).

  A shared adapter contract test runs each adapter against a fake
  transport.

---

## 14. Rate Limiting

### 14.1 Current [I / M]

* **Limits:**
  * login: 10 per IP and 5 per email, per 15 min;
  * register: 5 per IP per hour;
  * per user: graph 120/min, search 120/min, agent 30/h, message 120/h,
    ingest 20/h, write 120/min.
* **Failure mode:** fails closed. A PostgreSQL error returns 500
  (`rate_limit_service.py:90-106`).
* **Cost:** one counter operation is 1.46 ms p50 / 2.03 ms p95, adding
  1.5–4.2 ms per request [M Phase 9, single client].
* **Problems:**
  * **Shared per-IP bucket.** The Next rewrite passes client
    `X-Forwarded-For` verbatim, so the proxy must not be trusted
    (ADR 0026). The Render platform proxy adds another hop that uvicorn
    only trusts from 127.0.0.1 [I Procfile; A: Render forwarding
    behaviour]. The result is that per-IP login and register limits act as
    **one global bucket**.
  * **Targeted lockout.** The per-email limit lets anyone block logins for
    a known email for 15 minutes.
  * **Unlimited routes:** `POST /conversations` and all GET detail/list
    routes.

### 14.2 Design options for client-IP attribution (D5)

| Option | How | Trust basis | Verdict |
|---|---|---|---|
| A. Trust the proxy's XFF | Add the proxy to `TRUSTED_PROXY_IPS` | Vercel egress IPs aren't fixed, and the rewrite forwards client XFF | **Rejected** (spoofable) |
| B. Signed client-IP header | A Next.js `proxy.ts` (Next 16's middleware) reads the platform-provided client IP (on Vercel, `x-forwarded-for` / `x-real-ip` set by Vercel's edge [A: must verify Vercel overwrites client-supplied values]), and sets `X-Client-IP` + `X-Proxy-Auth: HMAC(secret, ip + timestamp)` on the rewritten request. The backend trusts `X-Client-IP` only with a valid, fresh HMAC | A shared secret between the two services | **Recommended if** the Vercel behaviour is verified in M12. Adds one small middleware file, no dependency |
| C. No per-IP; rely on per-account + per-email + global | Remove per-IP buckets behind the proxy; add a global login ceiling (e.g. 300 / 15 min) as a circuit breaker | None needed | **Fallback.** Honest and simple, but weaker against credential stuffing |

**Mitigating targeted lockout:** key the email bucket on
`(email, client_ip)` when a trusted IP exists (option B). Otherwise keep
the per-email limit and document the lockout risk [L]. Staging has
invite-only accounts, so the risk is low.

**Add limits:** `POST /conversations` → write class. GETs stay
unlimited per request, but are counted in metrics.

### 14.3 Measurement (M10)

The earlier numbers came from a single client. The new measurement:

* 10 / 25 / 50 concurrent virtual users against `/memories/search` (rate
  limited) and `/auth/me` (not limited);
* record the p50 / p95 of the counter operation;
* record connection-pool wait time. The pool is 5 + 5, and each limited
  request uses about 3 short transactions [I];
* record errors.

**Decision rule:** keep PostgreSQL counters unless counter p95 exceeds
10 ms or pool waits dominate at 25 users. The first fix would be pool
sizing, not Redis.

---

## 15. Security Hardening

| # | Area | Finding | Label | Action |
|---|---|---|---|---|
| S1 | Sessions | 7 d absolute / 24 h idle; SHA-256 stored; revocation on logout, logout-all, password change, disable | [I] + Phase 9 tests | Keep |
| S2 | Sessions | **Expired / revoked `auth_sessions` rows are never deleted** | [V: grep for DELETE in `auth_repository.py` is empty] | Sweeper deletes sessions expired or revoked more than 7 days ago, in batches |
| S3 | Hashing | scrypt N=2^15, r=8, p=3; production floor enforced; rehash on login | [I] | Keep. **Measure** login latency on the target instance size (M12); 0.1 vCPU hosts may be too slow [A] |
| S4 | Brute force | Fixed-window per-IP and per-email; no lockout | [I] | §14 |
| S5 | Enumeration | Login equalized with a dummy hash [M Phase 9]; register reveals `email_taken` (accepted Q7), off in production | [I] | Keep |
| S6 | Cookies | HttpOnly, SameSite=Lax, Path=/, host-only; Secure required in production | [I] + E2E [V] | Keep |
| S7 | CORS | Explicit origins; the optional preview regex `https://{project}(-[a-z0-9-]+)?\.vercel\.app` also matches **other** Vercel projects named `{project}-…` | [V: read `config.py:409`] | Anchor to the team scope `{project}-git-…-{team}.vercel.app`, or disable previews in staging. Default is off |
| S8 | CSRF | Origin check on cookie-authenticated unsafe methods; E2E 403 | [V] | Require `https://` origins in production (`localhost` excepted) |
| S9 | Headers | CSP, XFO DENY, nosniff, Referrer-Policy present; **no HSTS, no Permissions-Policy** | [I] | Add `Strict-Transport-Security: max-age=31536000` (Vercel is always HTTPS [A: verify]) and a restrictive `Permissions-Policy` in `next.config.ts`; add the same headers on backend responses |
| S10 | Upload | `await file.read()` loads the whole body; the size check runs after; multipart is parsed before auth | [V: `routes/documents.py:121`; A: parse-before-auth ordering] | Reject on `Content-Length` > limit in middleware before parsing. Read in chunks with a running cap, and abort past the limit. Measure memory with a 20 MB and a 200 MB request |
| S11 | Upload | Content-sniffed type; display-only filename sanitization; storage key is a UUID; per-user dirs with a strict regex | [I] | Keep |
| S12 | Storage I/O | Synchronous file I/O in async methods | [I] | `asyncio.to_thread` |
| S13 | PDF | Page cap before extraction; no decompression-bomb guard; timeout thread unkillable | [I] | J8 semaphore; cap extracted characters per page and in total, then abort |
| S14 | Prompt injection | Retrieved content is data (ADR 0029, agent structural check PASS [M Phase 10]) | [V] | Keep; covered by the eval |
| S15 | Isolation | Cypher audit, SQL audit and route audit are static tests; cross-user HTTP tests for memory, graph, documents and conversations | [V] | Add cross-user HTTP tests for `/review/*` and `/extraction/jobs*` (M8) |
| S16 | Worker ticks | A user's request processes up to 3 queued jobs of **any** user, cross-user by design; results are written under each job's own user | [I] | Keep, but move processing to the sweeper so request latency doesn't depend on other users' jobs (§6) |
| S17 | Request id | Unvalidated and unbounded | [I] | §9 |
| S18 | `/docs`, `/openapi.json` | Always exposed | [I] | `EXPOSE_DOCS` setting, default off in production (D8) |
| S19 | Secrets | `.env` ignored; no secret scanning | [V] | gitleaks in CI (§22) |
| S20 | Dependencies | No audit | [V] | `pip-audit` (via `uvx`) and `npm audit --omit=dev` in CI |

---

## 16. Document Storage

**Key fact:** raw bytes are read **only** by ingestion
(`documents/ingestion.py:180`, the single `storage.get` call site).
Provenance, chunks and memories live in PostgreSQL and Neo4j
(`provenance_service.py:70-100`). [I] Losing bytes **after** ingestion
finishes loses nothing the product shows. It only prevents re-ingestion.

| Option | Durability | Cost | Complexity | Verdict |
|---|---|---|---|---|
| 1. Persistent disk | Good | Not available on Render Free [P] | Low | Not possible at zero cost |
| 2. Object storage (S3 API) | Good | R2: 10 GB free [P], card likely required (ambiguous). B2: 10 GB free [P]. Supabase: 1 GB, 50 MB max upload, **pauses after a week idle** [P] | New adapter + SDK dependency + credentials + signed requests | **Production path**, deferred to D3 |
| 3. PostgreSQL `bytea` | Good (backed up with PG) | Neon Free 0.5 GB total [P]; 20 MB files would exhaust it after about 25 uploads | Low | **Rejected** for staging capacity |
| 4. **Transient local bytes (demo / staging)** | Bytes survive until ingestion completes, or until restart | 0 | Smallest | **Recommended for staging** |

**Recommended design for staging (option 4, made honest):**

* After ingestion reaches a terminal state, **delete the raw bytes**.
  Nothing needs them, and it bounds disk use on the ephemeral filesystem.
* If the bytes are missing when a job runs (restart before ingestion), the
  document goes to `FAILED` with `document_missing`. That error code
  already exists in the UI copy [I `frontend/lib/format/documents.ts`].
  The user re-uploads. Duplicate detection is by content hash, so the
  re-upload gets the same document once the failed one is handled; a test
  must cover re-upload after `document_missing`.
* The storage protocol stays, so an S3-compatible adapter can be added
  later without other changes.
* `DOCUMENT_STORAGE_DIR` must be absolute in production-like environments.

**Upload path size (open, must be measured in M12).** Vercel functions
cap bodies at 4.5 MB [P]. The limit for pure external rewrites is not
documented [P: unverified]. If a 20 MB multipart upload through the Vercel
rewrite fails, the choice is between:

* (a) lower `DOCUMENT_MAX_BYTES` to 4 MB in staging (smallest change), or
* (b) direct-to-backend upload. This breaks same-origin cookies, so it is
  rejected.

---

## 17. Performance & Load Testing

Phase 10 measured **one local user**. That is not load testing. [M]

**Tool.** A small asyncio + `httpx` load script, `backend/eval/load_test.py`.
`httpx` is already a dev dependency, so nothing new is needed. It gives
fixed-concurrency closed-loop virtual users, a warm-up, a duration and
per-endpoint latency lists, and outputs JSON.

**Dataset.** The existing seed plus the `e2e_seed.py --hub-size 200`
style generator, extended to create N users × M memories. The default is
20 users × 200 memories. Enormous datasets are not useful here.

| Scenario | Concurrency | Metrics |
|---|---|---|
| Login (production scrypt cost) | 1 / 5 / 10 | p50, p95, errors, CPU. scrypt is CPU-bound and runs in threads [I] |
| Session validation (`/auth/me`) | 10 / 25 / 50 | p50, p95, throughput |
| Memory search, graph overview, graph 2-hop, provenance, documents list | 10 / 25 / 50 | p50, p95, error rate, rate-limit 429s (limits raised for the test) |
| Agent turn (FakeLLM) | 1 / 5 / 10 | p50, p95. DB session hold time and pool waits (§6.6) |
| Extraction throughput (FakeLLM) | Sweeper, 1 instance | Jobs/min, claim latency, queue-drain time for 500 jobs |
| Ingestion throughput | 10 txt + 5 PDFs | Docs/min, parse time, the semaphore effect |
| PostgreSQL | during the above | Pool wait (instrumented), slow queries (`log_min_duration_statement`) |
| Neo4j | during the above | Query latency via our graph-latency histogram |

**Output:** a table with p50, p95, error rate, throughput and the observed
bottleneck (CPU, pool, Neo4j or LLM), clearly labelled **"local Apple M1,
not a capacity claim"**. The same script runs once against staging in M12
at **low** concurrency (≤5), because free-tier quotas are shared.

---

## 18. Graph Performance

The server budget is `max_nodes` 150 by default. **Do not raise it**
(Phase 8 contract). 300 and 600 elements arise only through client
expansion (the canvas cap is 600).

**End-to-end measurement at 50 / 150 / 300 / 600 elements**, extending
`frontend/e2e/perf.spec.ts`. Each run records four stages separately:

1. **backend** query time (server timing header added to graph routes,
   `Server-Timing: db;dur=…`);
2. **network**: response time minus server time, plus payload size;
3. **layout**: cose time, already reported by `onRendered`;
4. **paint**: a `requestAnimationFrame` pair after `layoutstop`.

50 and 150 come from hub fixtures of that size. 300 and 600 come from
successive expansions. The result identifies the bottleneck per size.

Phase 10 measured cose at 102 / 686 / 1609 ms (headless, 150 / 400 / 600
elements) [M], so layout is the expected bottleneck above 300. Possible
follow-ups, **only if measured** and each requiring approval:

* a cheaper layout for incremental expansions (e.g. `concentric` for the
  new nodes only);
* moving layout to a web worker.

---

## 19. Frontend Performance

**Measured in Phase 10:** the authenticated `/memories` route, cold
cache, transfers **190.9 KB gzip** against a 180 KB budget. `/login`
alone is 149.3 KB. [M]

Chunk sizes at `a9da2db` (gzip -9 of the build output) [M]:

| Chunk | Size (gzip) |
|---|---|
| react-dom | ~71 KB |
| Next runtime chunks | ~44 KB + ~39 KB |
| Cytoscape (lazy, not on `/memories`, verified in Phase 10) | 134 KB |

The framework floor is therefore about 154 KB. App code plus shared
libraries on `/memories` is about 37 KB, and the target means removing
at least 11 KB of it.

**Method (M10):**

1. **Attribute first.** Build with `productionBrowserSourceMaps`
   (temporary, not committed) and map the bytes of each `/memories`
   chunk to modules, with a small Node script over the source maps. No
   new dependency.
2. **Candidates to test, one at a time, measuring each:**
   * Does the `(app)` layout pull review/graph/document code into the
     shared chunk?
   * Split the `AppShell` / `TimeLensControl` / `ReviewBadge` imports.
   * `next/dynamic` for dialogs (`RetractDialog`, `Dialog`) that aren't
     visible on first paint.
   * Check whether TanStack Query devtools or unused `lib/format` maps
     are bundled.
3. **Preserve:** Cytoscape stays out of every non-graph route (asserted
   by a Playwright check on loaded chunk names).
4. **If the target can't be met without harming UX**, record the measured
   floor and propose revising the budget with evidence (D9). Do not
   remove functionality to hit a number.

---

## 20. Search Reliability

**Root cause of the Phase 10 bug [V].** The route spread a dataclass
(`**vars(...)`) into an `extra="forbid"` Pydantic model after the
dataclass gained fields. Empty results never touched the code path. It is
fixed and pinned by `tests/unit/test_search_route.py`, which fails without
the fix.

**Same risk class elsewhere [I / V]:**

* `ReviewAuditLogEntry(**row)` (`review_service.py:181`) and
  `SweepResult(**counts)` (`routes/extraction.py:89`) spread dicts into
  forbid models. The keys match today; both are fragile.
* **No HTTP tests with populated responses** exist for:
  * all 12 `/review/*` routes (only service-level tests);
  * `/extraction/jobs`, `/extraction/jobs/{id}`;
  * the three `/internal/*` routes.

**Plan (M8):** targeted tests, no refactor.

1. A contract test per route above: seed one populated item, call the
   route, assert 200 and validate against the response model. Cross-user
   404 or empty is asserted in the same test.
2. Replace the two spreads with explicit field mapping (as in the search
   fix). This keeps the forbid-model drift detection meaningful.
3. A **route inventory test** fails when a new route has no populated
   contract test. It uses a registry of `(method, path)` → test id and is
   in the spirit of the existing route audit.

---

## 21. Evaluation Reliability

**Current [I]:**

* Deterministic evaluations (`runner.py`, `retrieval_runner.py`,
  `graph_api_runner.py`, deterministic sections of the document and agent
  runners) use fakes.
* **Real-LLM sections run implicitly** whenever `LLM_PROVIDER != fake`
  (`agent_runner.py:401`, `document_runner.py:905`). That caused the
  unintended Gemini calls in Phase 10.
* Phase 7 persistence (`eval/persistence.py`) resumes by
  harness/dataset/mode/provider/model. It skips completed tasks, stores
  RATE_LIMITED tasks, and finalizes only when every task is terminal.
  **This is sufficient** as a checkpoint framework.
* 429 is detected by substring (`"429" in error`), which is fragile. The
  document runner has no pacing.

**Plan (M3):**

1. **Explicit opt-in.** Real-LLM sections run only with `--real-llm` (or
   `EVAL_REAL_LLM=1`). Without it they print "skipped (opt-in)", even if
   `.env` configures Gemini.
2. Detect rate limits by exception type (`LLMRateLimitedError`, §12) and
   honour its `retry_after` before stopping.
3. Reports label runs **INCOMPLETE** with counts
   (completed / rate-limited / pending). Never average incomplete runs
   into accuracy. Existing behaviour is kept and asserted by a test.
4. CI runs only the deterministic evaluations: graph conformance and the
   retrieval regression byte-comparison, in the integration workflow.
5. Fix the pre-existing `eval/runner.py` FK cleanup: delete
   `memory_review_candidates` before `extraction_candidates`. It is small
   and blocks a clean deterministic evaluation.

---

## 22. CI/CD

**Current:** `ci.yml` covers ruff, unit tests (py3.12/3.14), the isolation
audits, the integration-skip guard and the frontend job.
`integration.yml` is manual/weekly with managed-database secrets. [I]
**It has never run on GitHub**, since there is no remote. [V]

| Addition | Where | Cost | Include? |
|---|---|---|---|
| gitleaks secret scan (free action, or `gitleaks` binary) | ci.yml | Seconds | **Yes** |
| `pip-audit` via `uvx` (no project dependency) | ci.yml | ~30 s | **Yes**, fail on known vulnerabilities with a fix available |
| `npm audit --omit=dev --audit-level=high` | ci.yml | Seconds | **Yes** |
| Migration checks (single head, upgrade + `alembic check`, HNSW present) | integration.yml (needs PostgreSQL) | ~1 min | **Yes** |
| Unit test that `include_object` excludes the HNSW index | ci.yml | 0 | **Yes** |
| Deterministic evaluations (graph conformance, retrieval byte-compare) | integration.yml | ~2 min | **Yes** |
| Playwright E2E | integration.yml (needs both databases + a build) | ~5 min | **Optional**, manual trigger (D11) |
| Backend type checking (mypy/pyright) | – | New dependency, many findings | **Defer** |
| Deployment automation | – | Only meaningful with a remote + hosting accounts | **Design only**. Render and Vercel auto-deploy from the default branch once connected. Migrations stay a manual gate (§24) |
| Scheduled keep-alive / backup workflows | new `ops.yml` | Free for public repos; 2,000 min/month for private [P] | **Yes, if D10**. Note: public-repo cron is disabled after 60 days without activity [P] |

---

## 23. Deployment Configuration

**Backend, validated at startup when `APP_ENV` is production-like.** Every
new check refuses to start with a clear message, and each gets a unit
test.

| Variable | Rule | New? |
|---|---|---|
| `DATABASE_URL`, `NEO4J_URI/USERNAME/PASSWORD` | Required (today only logged) | **New** |
| `AUTH_MODE=session`, `ALLOW_USER_ID_HEADER=false`, Secure cookie | Required | Existing |
| `FRONTEND_URL` / `CORS_ORIGINS` | Required; each origin `https://` (except `localhost` / `127.0.0.1`) | Scheme check **new** |
| `INTERNAL_API_TOKEN` | Required, ≥32 characters | **New** |
| `LLM_PROVIDER` / `EMBEDDING_PROVIDER` | Must have an implemented adapter and a key. `fake` refused **unless** `ALLOW_FAKE_AI=true` (explicit staging/demo opt-in, logged at startup as a warning) (D7) | **New** |
| `DOCUMENT_STORAGE_DIR` | Absolute path | **New** |
| `DEBUG`, `DATABASE_ECHO` | Must be false | **New** |
| `LOG_LEVEL` | Not DEBUG | **New** |
| `LOG_FORMAT` | Default json in production-like environments | **New** |
| `TRUSTED_PROXY_IPS` | May be empty. Warn if it contains a public-range CIDR wider than /24 | **New** (warning) |
| `EXPOSE_DOCS` | Default false in production-like environments | **New** (D8) |
| `AUTH_REGISTRATION_ENABLED` | Default off | Existing |
| `PROXY_SHARED_SECRET` | Required if option B (§14) | **New**, conditional |
| `SWEEP_INTERVAL_SECONDS` | Default 60; 0 disables | **New** |

**Process command** (`Procfile` / Render start command):

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-access-log --timeout-graceful-shutdown 20
```

`--proxy-headers` stays at its default; **no `--forwarded-allow-ips`**
unless option B proves a trust chain. On shutdown, the sweeper finishes
its current job or releases its lease.

**Frontend:** `next.config.ts` **fails the production build** if
`BACKEND_ORIGIN` is unset or not `https://`, instead of silently falling
back to localhost [I `next.config.ts:9`].

**Clean-up:**

* Remove the dead `REDIS_URL` / `STORAGE_*` placeholders from
  `.env.example`, or mark them "unused".
* Fix the FastAPI description and version, and the health `phase`.

---

## 24. First Deployment Procedure

"The server starts" is **not** success. Each step has a pass criterion,
and the run is recorded in a deployment log (`docs/DEPLOYMENT_LOG.md`).

| # | Step | Pass criterion |
|---|---|---|
| 1 | Provision Neon (project, region near Render), AuraDB Free, Render web service, Vercel project | Accounts exist. **Owner action, not automatable by the agent** |
| 2 | Configure secrets in each dashboard (never in the repo) | `APP_ENV=production` boot passes validation (§23) |
| 3 | `alembic upgrade head` from the operator machine against Neon | `alembic current` = head; HNSW index present (`pg_indexes`); pgvector version recorded |
| 4 | `init_graph.py` against Aura | Constraints listed; the Cypher-compatibility smoke (`COLLECT {}`, `CALL () {}`) passes |
| 5 | Deploy the backend; check `/health/ready` | 200 with postgres / neo4j / migrations all ok |
| 6 | `manage_users.py create --email <owner>` | Account created; password via prompt |
| 7 | Deploy the frontend with `BACKEND_ORIGIN=https://<render>` at build | Build passes the §23 check |
| 8 | Log in through the UI | Cookie HttpOnly / Secure / Lax (DevTools); `/auth/me` 200; `Cache-Control: no-store` |
| 9 | Graph access | Overview + neighbourhood render on seeded demo data (`seed_demo.py --user-id <owner id>`) |
| 10 | Document upload | 1 MB txt → `COMPLETE`. **Measure the 20 MB upload through the rewrite** (§16) and apply the decision |
| 11 | Extraction | Send a message; within one sweep interval the memory appears |
| 12 | Search | `POST /search` returns populated results (the regression) |
| 13 | Agent | One agent turn (fake, or a free key per D7) returns an answer with citations or an honest `not_found` |
| 14 | Backup | Run the backup workflow/script; artifacts exist; **restore drill** (§7.3) into local stores passes |
| 15 | Rollback | Redeploy the previous Render deploy (platform rollback) and confirm readiness. Migration rollback is **not** offered (forward-only); the procedure is fix-forward |
| 16 | Failure drills subset on staging | LLM unavailable, backend restart during extraction, expired session |
| 17 | Sign-off | Checklist (§26) updated with evidence links |

---

## 25. Failure Drills

Run locally in M13 with the same scripts, and a subset on staging (§24
step 16). Each drill has a script or test and an expected outcome.

| Drill | How | Expected behaviour | Must not happen |
|---|---|---|---|
| PostgreSQL unavailable | Stop PostgreSQL during load | Readiness 503; requests 503 with `database_unavailable`; sweeper logs and backs off; recovers on restart with no manual step | 500 with SQL text; stuck `in_progress` jobs after recovery |
| Neo4j unavailable | Stop Neo4j mid-extraction | Extraction job `failed` → retried after recovery; memories created exactly once (≤1 duplicate, documented) | Candidates marked INVALID (J1) |
| LLM unavailable | Fake transport raises ConnectError | Adapter retries, then job `failed`; agent 503 `llm_unavailable` | Raw SDK text to clients; Neo4j writes |
| LLM 429 | Fake 429 with Retry-After | Wait honoured; cooldown engages; attempts not consumed; abandoned after 24 h (simulated clock); requeue works | Hammering the provider; fabricated evaluation results |
| Restart during extraction | Kill `-9` the API mid-job | On boot, the sweeper reclaims after lease expiry; job completes | Job stuck forever; two owners writing |
| Ingestion interrupted | Kill during parse, and after the chunk jobs commit | Resume reaches a terminal document state (J2, J6) | FK violation loop; document stuck in `parsing` |
| Bytes missing | Delete the stored file before ingestion | Document FAILED `document_missing`; re-upload works | Crash; silent pending |
| Expired session | Set `expires_at` in the past | 401 → UI redirects with the "session ended" notice | Data from the cache shown after logout |
| Rate-limit exhaustion | Exceed search / login limits | 429 with `Retry-After`; UI countdown | 500; other users affected (except the documented shared IP bucket) |
| Invalid migration | Point readiness at a DB one revision behind | Readiness 503 `migrations: mismatch`; no traffic routed | Serving with a mismatched schema |
| Frontend / backend version mismatch | Old frontend build against a backend with a changed response field | CI OpenAPI drift catches it before deploy. At runtime, unknown fields are ignored and a missing required field shows an error state (the error boundary, §4.2) | A blank page |
| Upload too large | 25 MB and 200 MB bodies | 413 before full read; memory flat | OOM; process restart |

---

## 26. Production Readiness Checklist

PASS means evidence exists today. Most items are FAIL now **by design**:
this is the baseline Phase 11 works from. "Target" names the milestone
that should flip the item.

| Area | Item | Now | Target |
|---|---|---|---|
| SECURITY | No raw HTML rendering; lint + CI + tests | PASS [V Phase 10] | – |
| SECURITY | CSP, XFO, nosniff, Referrer-Policy | PASS [V] | – |
| SECURITY | HSTS + Permissions-Policy | FAIL | M6 |
| SECURITY | Secret scanning + dependency audit in CI | FAIL | M11 |
| SECURITY | Error bodies free of internals | FAIL (3 leaks, §11) | M4 |
| SECURITY | Upload size enforced before full read | FAIL | M6 |
| SECURITY | Preview-origin regex tight | FAIL (opt-in, off by default) | M6 |
| RELIABILITY | Infrastructure errors never become INVALID memories | FAIL (J1) | M1 |
| RELIABILITY | All queued work recovered automatically after a crash | FAIL (J2, J3) | M2 |
| RELIABILITY | Lease ownership enforced | FAIL (J4) | M1 |
| RELIABILITY | Every job reaches a terminal state | FAIL (J5, J7) | M1 |
| RELIABILITY | Ingestion retry after a partial commit | FAIL (J6) | M1 |
| RELIABILITY | Error boundaries in the frontend | FAIL | M4 |
| DATA DURABILITY | Canonical memory write is atomic per memory | PASS [I, `graph/connection.py:152-190`] | – |
| DATA DURABILITY | Document bytes policy explicit and honest | FAIL (implicit loss) | M6 |
| OBSERVABILITY | Structured, correlated logs without sensitive data | FAIL | M5 |
| OBSERVABILITY | Metrics (queue depth, errors, LLM) | FAIL | M5 |
| PERFORMANCE | Load characterization at concurrency | FAIL (single-user only) | M10 |
| PERFORMANCE | Frontend JS budget | FAIL (190.9 / 180 KB) | M10 (or D9) |
| PERFORMANCE | Graph end-to-end attribution | FAIL (layout only) | M10 |
| BACKUPS | PostgreSQL backup procedure | FAIL | M9 |
| BACKUPS | Neo4j backup procedure | FAIL | M9 |
| RECOVERY | Restore drill passed, RTO measured | FAIL | M9 / M12 |
| AUTH | Session TTL / idle / revocation / scrypt / enumeration | PASS [V Phase 9 tests] | – |
| AUTH | Expired-session cleanup | FAIL | M2 |
| RATE LIMITING | PostgreSQL limiter correct, fails closed | PASS [V Phase 9] | – |
| RATE LIMITING | Per-IP attribution behind the proxy | FAIL (G4) | M6 (D5) |
| RATE LIMITING | Overhead measured at concurrency | FAIL | M10 |
| LLM RELIABILITY | Retry-After, transport errors, cooldown | FAIL | M3 |
| LLM RELIABILITY | Provider failure cannot write memory | PASS by design [I]; test pending | M3 (test) |
| LLM RELIABILITY | Real-LLM evaluations explicitly opt-in | FAIL | M3 |
| DOCUMENT STORAGE | Durable object storage | DEFERRED (D3; staging uses transient bytes) | – |
| MIGRATIONS | HNSW index protected from autogenerate | FAIL | M7 |
| MIGRATIONS | Single head + drift check in CI | FAIL | M7 |
| MIGRATIONS | Forward-only policy documented | FAIL | M7 |
| FRONTEND | Build fails without `BACKEND_ORIGIN` | FAIL | M4 |
| FRONTEND | E2E + a11y suite | PASS [V Phase 10, local] | – |
| CI | Unit tests, lint, isolation audits, frontend build | PASS locally [V]; never run on GitHub | D10 |
| CI | E2E in CI | DEFERRED (D11) | – |
| DEPLOYMENT | Production-like config validated at startup | FAIL (§4.1) | M4 |
| DEPLOYMENT | First deployment executed with all §24 checks | FAIL | M12 (needs owner accounts) |
| DEPLOYMENT | Real production environment | NOT APPLICABLE (out of scope) | – |
| TESTING | Backend suite (1270), frontend (63 + 29), graph conformance | PASS [M Phase 10] | – |
| TESTING | Failure drills executed | FAIL | M13 |
| TESTING | Populated contract tests for every route | FAIL (review / extraction / internal) | M8 |

---

## 27. Milestone Breakdown

Ordered by risk: data-loss bugs first, deployment last. Each milestone
follows the project rule (plan → implement → tests → verify → report) and
must leave the suite green.

| # | Milestone | Content | Exit evidence |
|---|---|---|---|
| **M0** | Baselines | Load-test script (§17); frontend bundle attribution (§19); graph E2E stage timing (§18); queue claim benchmark (§6.3). **Measure before changing** | Baseline JSON files + a measured-baselines appendix added to this plan |
| **M1** | Job correctness | J1, J4, J5, J6, J7, J9, J10, J11 + tests | New tests fail on `a9da2db` and pass after; the full suite is green |
| **M2** | Recovery automation | In-process sweeper with an advisory lock (J2, J3); `scripts/run_sweep.py`; session cleanup (S2); J8 semaphore | Kill -9 drill recovers without manual steps |
| **M3** | LLM resilience + evaluation opt-in | §12 design; §13 startup rejection; §21 items 1–3 and 5 | Fake-transport tests; zero Neo4j writes on LLM failure; evaluations skip real-LLM runs without the flag |
| **M4** | Config, health, errors | §23 validation; §10 readiness (reconnect, migration state, codes only); §11 leak fixes + envelope for `HTTPException`; request-id validation; frontend error boundaries + build guard | Validation unit tests; leak test; readiness tests |
| **M5** | Observability | JSON logs, contextvars, access line with route template, no uvicorn access log, redaction test; `/internal/metrics` | Log-redaction test; metrics endpoint test |
| **M6** | Security + storage | S7, S9, S10, S12, S13; §14 (D5 option); POST /conversations limit; §16 transient-bytes policy | Upload memory measurement; header E2E; `document_missing` re-upload test |
| **M7** | Migration safety | `include_object`; migration tests; policy docs | `alembic check` clean; HNSW survives |
| **M8** | Contract tests | §20: populated tests for review / extraction / internal; explicit mappings; route inventory test | The inventory test fails on any new untested route |
| **M9** | Backups + restore | `backup.sh`, `export_graph.py`, `import_graph.py`, `check_consistency.py`; local restore drill; measured RTO | Drill log with timings |
| **M10** | Performance | Load characterization (§17); rate-limit overhead at concurrency (§14.3); graph attribution (§18); frontend optimization or budget decision (§19) | Before/after tables; no budget increase |
| **M11** | CI additions | §22 (gitleaks, pip-audit, npm audit, migration and evaluation checks in integration) | Workflows validated locally (`act` is **not** used; syntax check + dry run). They run on GitHub only after D10 |
| **M12** | Staging deployment (**owner-gated**) | Deployment artifacts (`render.yaml` optional; Procfile update; Vercel settings doc); §24 procedure executed with the owner, who provisions the accounts | Deployment log; checklist items flipped with evidence |
| **M13** | Failure drills + final report | §25 locally (all) and on staging (subset); final checklist | Drill results table; honest final report |

**Dependencies:**

* M1 must precede M2: the sweeper must not scale up buggy jobs.
* M3 precedes M13's LLM drills.
* M4 precedes M12.
* M12 requires D1, D2, D10 and the owner's accounts.

---

## 28. Explicit Non-Goals

* New AI features, providers, or memory-model changes.
* Redis, Celery, Kafka or any new stateful service, unless M0/M10 measure
  a need (§6.3, §14.3).
* A separate worker service. The in-process sweeper is enough for one
  instance.
* A distributed circuit breaker. With one instance, per-process state is
  equivalent, and it is documented if scaling out.
* Real production deployment, SLOs, paid tiers, multi-region, autoscaling.
* A subprocess or sandbox PDF parser (J8 is bounded, not solved) [L].
* A nonce-based CSP (needs dynamic rendering, per ADR 0029). *Done later, in the
  2026-09-28 re-verification: see `docs/PHASE11_REPORT.md` and the ADR 0029 amendment.*
* Durable object storage in staging (designed, deferred per D3).
* Backend type checking (mypy/pyright), deferred.
* Changing graph budgets to improve benchmarks.
* Reversible (down) migrations in staging/production.

---

## 29. Risks & Open Questions

### 29.1 Decisions requiring approval

| # | Decision | Options | Recommendation |
|---|---|---|---|
| **D1** | First target | Private staging / public demo / both | **Private staging** first; public demo later behind §3.2 conditions |
| **D2** | Backend host | Render Free (sleep 15 min, no cron, no disk) / Koyeb Free (0.1 vCPU, sleep 1 h) | **Render Free**. Re-evaluate if M12 measures login > 2 s at production scrypt cost |
| **D3** | Document bytes | Transient local (staging) / R2 / B2 / Supabase | **Transient** for staging; object storage deferred to production |
| **D4** | Upload limit if the Vercel rewrite caps bodies | Lower to 4 MB / keep 20 MB | Decide from the M12 measurement; default to lowering |
| **D5** | Client-IP attribution | B (HMAC header) / C (no per-IP + global ceiling) | **B if** Vercel's client-IP header is verified non-spoofable; otherwise **C** |
| **D6** | Metrics format | JSON endpoint (no dependency) / `prometheus_client` | **JSON**, no dependency |
| **D7** | AI in staging | Fake (`ALLOW_FAKE_AI=true`) / Gemini free key | **Fake by default**; the owner may switch to a free key, accepting 429s |
| **D8** | API docs in staging | Hidden / exposed | **Hidden** (`EXPOSE_DOCS=false`) |
| **D9** | Frontend budget if not reachable | Keep 180 KB and keep optimizing / revise with evidence | Decide after M10 attribution |
| **D10** | Create a remote repository (GitHub) | Yes (enables CI, cron, backups artifacts) / No | **Yes, private**. Owner action. The agent will not create remotes or push without explicit instruction |
| **D11** | E2E in CI | Manual workflow / skip | Manual workflow in integration |
| **D12** | Backup storage | GitHub Actions artifacts (encrypted with a passphrase secret) / operator laptop | Artifacts + periodic operator download |

### 29.2 Risks

* **Free-tier policy changes.** All [P] facts can change without notice.
  Re-verify at M12.
* **Aura "activity" definition is unverified.** The keep-alive must be a
  real write (a heartbeat node under a system namespace, excluded from user
  data by the `user_id` scoping) [A]. It must not violate the Cypher audit.
  The heartbeat statement is added to the audited modules with explicit
  justification.
* **Vercel rewrite body limit is unknown** (D4).
* **Neon and Aura cold starts stack with the Render cold start.** The
  first request could take more than 60 s. The 120 s Vercel rewrite
  timeout [P] likely covers it [A]. Measure in M12.
* **The in-process sweeper only runs while Render is awake.** Jobs
  created just before spin-down wait for the next request. That is
  acceptable for staging [L]. An external keep-alive cron (D10) bounds the
  delay.
* **At most one duplicate memory per crash** remains accepted
  (PHASE2_HARDENING §2.2) [L].
* **scrypt on small shared CPUs** may make login slow (S3) [A].

---

## 30. Acceptance Criteria

Phase 11 is complete when:

1. **No silent data loss.** The drills in §25 for Neo4j, PostgreSQL,
   LLM, restart and ingestion outages pass. No candidate becomes INVALID
   because of an infrastructure error. Every job and document reaches a
   terminal state or is automatically retried.
2. **Automatic recovery.** After `kill -9`, all queued work completes
   without manual intervention within lease + sweep interval.
3. **LLM failure is graceful and honest.** Retry-After is honoured, the
   cooldown works, attempts are preserved on quota errors, and there are
   zero Neo4j writes on LLM failure. Real-LLM evaluations never run
   without an explicit flag.
4. **Configuration is safe.** Every §23 rule is enforced with a test. A
   production-like boot with any insecure default refuses to start. The
   frontend production build fails without `BACKEND_ORIGIN`.
5. **Nothing internal leaks.** The leak test passes for every error class.
   `/health/ready` returns codes only and checks the migration state.
6. **Observability exists.** JSON logs are correlated by `request_id`.
   The redaction test proves no passwords, tokens, keys, content or emails
   are logged. `/internal/metrics` reports queue depth, error rates and
   LLM errors.
7. **Migrations are safe.** `include_object` protects the HNSW index.
   `alembic check` is clean. Single head. The forward-only policy is
   documented.
8. **Backups are proven.** A restore drill from backup artifacts succeeds
   locally, with measured RTO / RPO and the consistency check at zero
   orphans. Free-tier non-guarantees are documented.
9. **Performance is characterized.**
   * Load results (p50/p95/errors/throughput/bottleneck) at 10/25/50
     users, labelled local.
   * Graph attribution at 50/150/300/600.
   * The frontend budget met, or revised with evidence (D9).
   * Graph budgets unchanged.
10. **Contracts are covered.** Populated contract tests exist for every
    route, enforced by the inventory test.
11. **Security items** S2, S7–S10, S12, S13, S17, S18 are done with tests.
    The §14 option is implemented per D5.
12. **CI** includes secret scanning, dependency audit and migration and
    deterministic-evaluation checks (running on GitHub if D10).
13. **Staging** (if approved and the owner provisions accounts): every
    §24 step passes with recorded evidence. It is described as staging,
    **never** production.
14. **Existing guarantees intact.**
    * The backend suite passes (≥1270 plus new tests).
    * Frontend 63+ / E2E 29+ pass.
    * Graph conformance 39/39.
    * Retrieval output byte-identical, unless a deliberate, documented
      change.
    * Deterministic document evaluation 10/10.
15. **The checklist (§26)** is updated. No item is marked PASS without an
    evidence reference.

---

## 31. Implementation Status (report, 2026-09-27)

Labels:
- **[V]** verified by a test or check that passed in this run;
- **[M]** measured, with the conditions stated;
- **[I]** inspected in code;
- **[A]** assumption;
- **[L]** limitation.

All measurements are local, on an Apple M1 with PostgreSQL 17 and Neo4j
Community 2026.08.1 and fake LLM/embeddings unless stated. They are **not**
staging or Render/Vercel figures. Nothing has been deployed. The system is
**not** production-ready.

### 31.1 Milestones

| # | Status | Evidence |
|---|---|---|
| M0 Baselines | DONE | §31.3 "M0" columns [M] |
| M1 Job correctness | DONE | `test_job_reliability.py` (J1–J7 and backfill) [V]; ADR 0031 |
| M2 Sweeper | DONE | `test_sweeper.py`, `test_sweeper_lifespan.py` [V]; drills 5–6 [M] |
| M3 LLM resilience | DONE | `test_llm_resilience.py`, `test_llm_job_resilience.py`, `test_eval_opt_in.py`, `test_offline_llm.py` [V] |
| M4 Config / health / errors | DONE | `test_production_config.py`, `test_readiness.py`, `test_error_leaks.py`, `errorBoundary.test.tsx` [V] |
| M5 Logs + metrics | DONE | `test_observability.py`, `test_log_redaction.py` [V] |
| M6 Security | DONE | `test_security_headers.py`, `test_upload_limits.py`, `test_secret_scan.py`, `test_rate_limit_attribution.py` [V]. pip-audit: no known vulnerabilities; npm audit: 0 [V] |
| M7 Migrations | DONE | `test_migrations.py` (fresh DB and existing DB), `test_migrations_static.py`; `alembic check` clean; single head [V]; policy in DEPLOYMENT §9 |
| M8 Contracts | DONE | `test_contracts.py`, `test_route_contract_inventory.py` [V] |
| M9 Backup / restore | DONE locally | §31.4 [M] |
| M10 Performance | DONE locally | §31.3 [M] |
| M11 CI | WRITTEN, never run on GitHub | `ci.yml`, `integration.yml`, `e2e.yml` (manual), `real-llm-eval.yml` (manual), `ops.yml`; no remote exists (D10) [L] |
| M12 Staging | **NOT PERFORMED** | No Render/Neon/Aura/Vercel accounts were provisioned. A local `APP_ENV=staging` dry run passed (§31.5) [L] |
| M13 Failure drills | DONE locally | §31.6 |

### 31.2 Final verification run

| Check | Result |
|---|---|
| Backend suite | 1413 passed, 0 failed, 109 s [V]. The two earlier final runs each had the same 2 failures, both test-only defects, fixed as below |
| Frontend unit tests | 69 passed [V] |
| Frontend typecheck, lint, production build | OK [V] |
| E2E (Playwright + axe) | 29/29 [V] |
| Graph API conformance | 39/39 [V] |
| Retrieval evaluation | Output identical to `a9da2db` (Recall 91.7 / 68.8 / 100) [V] |
| Document evaluation | 10/10 [V] |
| Agent evaluation | Real-LLM baseline not run (fake provider); prompt-injection check PASS [V] |
| Ruff check (`app tests eval ../scripts`) | Clean [V] |
| Secret scan | 244 files, 0 findings [V] |
| `alembic heads` / `alembic check` | 1 head; no new operations [V] |
| OpenAPI types | `lib/api/schema.d.ts` in sync [V] |
| Production build guards | Refuses a missing `BACKEND_ORIGIN` and a non-https one [V] |

The two defects fixed in the final run were both test-only; neither is a
product defect:
- **The rate-limit class test** created its conversation with
  `POST /conversations`, which Phase 11 made a rate-limited write. That call
  used up the write limit of 1.
- **The timed-sweeper test** read `passes` while the pass that had completed
  the job was still running its later steps.

`ruff format --check` is not a CI gate: 56 files were already unformatted at
`a9da2db`. All new Phase 11 files are formatted [V].

### 31.3 Performance (local) [M]

**Load.** Rate limiting was on with high limits, against the test database;
`eval/load_test.py`. Values are p50 / p95 in ms; 0 errors in every run.

| Endpoint | c=1 | c=10 | c=25 (M0) | c=50 (M10) |
|---|---|---|---|---|
| `GET /auth/me` | 2.4 / 5.7 | 9.9 / 17.0 | 25.5 / 163.5 | 175.5 / 676.1 (210 rps) |
| `GET /memories/search` | 8.6 / 14.3 | 36.1 / 83.8 | 95.9 / 137.9 | 223.1 / 628.5 (186 rps) |
| `GET /graph/overview` | 11.9 / 19.0 | 43.6 / 96.5 | 116.4 / 163.8 | 231.4 / 384.4 (205 rps) |

- **Throughput** plateaus at c≥10: one uvicorn process, CPU-bound on
  JSON/Pydantic plus driver round trips [I].
- **Latency** grows roughly linearly with concurrency beyond that point. It is
  queueing, not errors.
- **Login** (scrypt), p50 / p95: 194 / 200 ms at c=1, 240 / 271 at c=5,
  374 / 694 at c=10. This is deliberate hashing cost.
- **Rate-limit overhead:** +1 to +9 ms at c=1 and +9 to +12 ms at c=10.

**Jobs** (`eval/job_benchmark.py`):
- 819–1384 extraction jobs/min;
- claim p95 37–47 ms with 10 concurrent claimers;
- 0 duplicate claims.

**Graph end-to-end, median of 3** (`e2e/perf.spec.ts`):

| Size | Backend | Network | Layout | Paint |
|---|---|---|---|---|
| ~50 nodes (99 elements) | 87–108 ms | 8 ms | 64–88 ms | 25–28 ms |
| ~150 nodes (299 elements) | 134–276 ms | 4–22 ms | 431–481 ms | 34–44 ms |
| ~300 nodes (597 elements, after expansion) | expansion 85–279 ms | 6–9 ms | 1541–1550 ms | 45–47 ms |

- **Bottleneck:** the cose layout, not the API.
- **Headless layout** (`bench-graph-layout.mjs`): 100 elements 49 ms, 300 →
  390 ms, 600 → 1.6 s, 1200 → 6.4 s.
- **"600 nodes"** exceeds the 600-element canvas cap, so it is measured
  headless only [L].
- **Graph budgets:** unchanged.

**Frontend JS.** The metric is now split at `load`:

| Page / build | Critical | Prefetch |
|---|---|---|
| `/memories` cold, `a9da2db` | 160.1 KB | 33.7 KB |
| `/memories` cold, now | 168.5 KB | 30.9 KB |
| `/login`, now | 157.7 KB | – |

- `/login` was 149.3 KB at M0, under the Phase 10 metric.
- **Budget:** 180 KB, met.
- **The +8.4 KB** is attributed to the error boundaries (~4.8 KB)
  (`scripts/bundle-attribution.mjs`).
- **Warm navigation** is served from the browser cache (0 B, 20/20 cache
  hits). The Phase 10 code summed Chromium's negative cache-hit sizes; this
  is now fixed.

**LCP** (median): `/memories` 156 ms, `/login` 100 ms. Auth bootstrap to
first page: 141 ms.

### 31.4 Backup / restore drill (local) [M]

- **Backup:** `scripts/backup.py create` produced an encrypted PostgreSQL and
  Neo4j backup in 2.5 s. Restore plus schema took 4.96 s. `verify` returned
  ok, and `check_consistency.py` found 0 orphans.
- **Restored stack:** a backend started on the restored stores passed
  readiness. It served:
  - login;
  - the superseded memory with `valid_until`;
  - the "now" vs "March" history;
  - the review queue;
  - a document and its chunks;
  - hybrid search;
  - the graph neighbourhood.
- **RTO** is about 7.5 s of tool time at this data size [M].
- **RPO** equals the backup schedule. It is not measured on staging [L].
- **Free tiers** give no platform backups we can rely on (DEPLOYMENT §6) [L].

### 31.5 Staging dry run and upload path (local) [M]

**Dry run** with `APP_ENV=staging`:
- readiness 200;
- `/docs` 404 (D8);
- HSTS present;
- JSON logs;
- a misconfigured boot is refused and lists every problem at once.

The disabled `/auth/register` with an empty body returns 422, not 404
(pre-existing, harmless) [L].

**Upload path** through `next start`:

| Upload | Result |
|---|---|
| 1 MiB, 4 MiB, 20 MiB (max) | 201 |
| max + 1 B | 422 `file_too_large` |
| max + 200 KB | 413 direct to the backend; 500 via the Next proxy (early close) |

- **Proxy limit (found here):** the proxy's default 10 MB body limit
  silently truncated 20 MiB uploads. It is fixed with
  `proxyClientMaxBodySize`.
- **Over-limit via the proxy:** the client-side size check prevents this
  case in the UI [L].
- **Vercel's rewrite limit is not measured** (D4). Measure it at M12 before
  keeping 20 MiB [L].

### 31.6 Failure drills (numbering as §25)

Live drills were run with `scripts/failure_drills.py`: a real API
subprocess, with PostgreSQL and Neo4j behind TCP proxies, on the test
database only.

| # | Drill | Trigger | Expected | Actual | Result | Follow-up |
|---|---|---|---|---|---|---|
| 1 | PostgreSQL unavailable | Cut the PG TCP path live | Liveness 200; readiness 503; requests 503, no internals; self-heals | Live 200; ready 503 (`postgresql: unavailable`); request 503 `database_unavailable`; no leak; healed; then 200 | PASS [M] | Before the fix: 500. Raw asyncpg connection errors were unmapped (pre-existing); fixed in `db/engine.py` and `test_db_error_mapping.py` |
| 2 | Neo4j unavailable | Cut the bolt path live | Readiness 503; write 503 `graph_unavailable`; heals | Ready 503; write 503; graph 503; no leak; healed; write after 201 | PASS [M] | Extraction during the outage is retried, not INVALID: J1 tests [V] |
| 3 | LLM unavailable | Fake transport errors | Retries, then job failed; agent 503; no Neo4j writes | As expected | PASS [V] | Fault-injection tests, not live (D7) |
| 4 | LLM 429 | Fake 429 plus Retry-After | Wait honoured; cooldown; attempts kept; terminal after 24 h; requeue | As expected | PASS [V] | `test_llm_job_resilience.py`, `test_requeue_jobs.py` |
| 5 | Restart during extraction | `kill -9` the API; a second process claims the job and is killed | Reclaimed after the lease; completes | Held by `doomed-process:1`; done in 33.6 s (30 s lease) | PASS [M] | – |
| 6 | Ingestion interrupted | Job in progress and document "parsing" with a dead owner | Sweep reclaims; document progresses | Reached `extracting` in 33.6 s | PASS [M] | FK retry path: J6 test [V] |
| 7 | Bytes missing | File deleted before ingestion | FAILED `document_missing` | As expected | PASS [V] | Test only |
| 8 | Expired session | `expires_at` in the past | 401; other sessions unaffected | 401 `authentication_required`; other 200 | PASS [M] | UI redirect: E2E [V] |
| 9 | Rate-limit exhaustion | 22 searches against 20/min | 429 plus Retry-After for that user only | First 429 at request 21; Retry-After 6; other user 200 | PASS [M] | The UI countdown was not exercised in this drill |
| 10 | Invalid migration | API on a DB at an older revision | Readiness 503 `migrations=mismatch` | 503 `migrations: mismatch` | PASS [M] | Scratch DB dropped |
| 11 | FE/BE version mismatch | Changed a response field | CI OpenAPI drift fails; the UI error boundary catches it | Drift detected; revert restores clean | PASS [V] | Runtime: `errorBoundary.test.tsx` |
| 12 | Upload too large | 200 MiB streamed and declared bodies | 413 before full read; memory flat | 413; peak allocation under 16 MB asserted | PASS [V] | Via the Next proxy: §31.5 [L] |

### 31.7 Production readiness checklist (updated from §26)

| Area | Item | Status | Evidence |
|---|---|---|---|
| SECURITY | No raw HTML rendering | PASS | Phase 10 lint/tests; E2E inert-rendering test [V] |
| SECURITY | CSP, XFO, nosniff, Referrer-Policy | PASS | `test_security_headers.py` [V] |
| SECURITY | HSTS + Permissions-Policy | PASS | `test_security_headers.py`; dry run [V] |
| SECURITY | Secret scanning + dependency audit | PASS locally; CI jobs never run on GitHub | `secret_scan.py`, pip-audit, npm audit [V] |
| SECURITY | Error bodies free of internals | PASS | `test_error_leaks.py`; drills 1–2 [V] |
| SECURITY | Upload size enforced before full read | PASS | `test_upload_limits.py` [V] |
| SECURITY | Preview-origin regex tight | PASS | Team-scoped regex; `test_auth_unit.py` [V] |
| RELIABILITY | Infrastructure errors never INVALID | PASS | J1 tests [V] |
| RELIABILITY | Queued work recovered after a crash | PASS | `test_sweeper.py`; drills 5–6 [V][M] |
| RELIABILITY | Lease ownership enforced | PASS | J4 test [V] |
| RELIABILITY | Every job reaches a terminal state | PASS | J5 and J7 tests [V] |
| RELIABILITY | Ingestion retry after a partial commit | PASS | J6 test [V] |
| RELIABILITY | Frontend error boundaries | PASS | `errorBoundary.test.tsx` [V] |
| DATA DURABILITY | Atomic canonical memory write | PASS | Phase 10 [I] |
| DATA DURABILITY | Document bytes policy explicit | PASS | D3 in DEPLOYMENT §2 and `DOCUMENT_DELETE_BYTES_AFTER_INGESTION` [I] |
| OBSERVABILITY | Correlated, redacted logs | PASS | `test_log_redaction.py`, `test_observability.py` [V] |
| OBSERVABILITY | Metrics | PASS | `/internal/metrics`; contract test [V] |
| PERFORMANCE | Load at concurrency | PASS (local) | §31.3 [M] |
| PERFORMANCE | Frontend JS budget | PASS | 168.5 KB critical against 180 KB [M] |
| PERFORMANCE | Graph end-to-end attribution | PASS up to ~300 nodes; 600 headless only | §31.3 [M][L] |
| BACKUPS | PostgreSQL and Neo4j procedures | PASS (local) | `scripts/backup.py`; §31.4 [M] |
| RECOVERY | Restore drill, RTO measured | PASS locally; staging DEFERRED | §31.4 [M] |
| AUTH | Session lifecycle | PASS | Phase 9 tests [V] |
| AUTH | Expired-session cleanup | PASS | Sweeper sessions step; test [V] |
| RATE LIMITING | Limiter correct, fails closed | PASS | Phase 9 tests [V] |
| RATE LIMITING | Per-IP attribution behind the proxy | PASS per D5: no false per-IP identity; global login/register limits | `test_rate_limit_attribution.py` [V] |
| RATE LIMITING | Overhead at concurrency | PASS | §31.3 [M] |
| LLM RELIABILITY | Retry-After, transport errors, cooldown | PASS | `test_llm_resilience.py` [V] |
| LLM RELIABILITY | Provider failure cannot write memory | PASS | `test_no_llm_failure_writes_to_neo4j` [V] |
| LLM RELIABILITY | Real-LLM evaluations opt-in | PASS | `test_eval_opt_in.py` [V] |
| DOCUMENT STORAGE | Durable object storage | DEFERRED (D3) | – |
| MIGRATIONS | HNSW index protected | PASS | `test_migrations_static.py`; `alembic check` [V] |
| MIGRATIONS | Single head + drift check in CI | PASS locally | `integration.yml` [I]; commands [V] |
| MIGRATIONS | Forward-only policy documented | PASS | DEPLOYMENT §9 |
| FRONTEND | Build fails without `BACKEND_ORIGIN` | PASS | Config guard [V] |
| FRONTEND | E2E + a11y | PASS (local) | 29/29 [V] |
| CI | Workflows | PASS locally; never run on GitHub | D10 [L] |
| CI | E2E in CI | Manual workflow only (D11) | `e2e.yml` [I] |
| DEPLOYMENT | Production-like config validated at startup | PASS | `test_production_config.py`; dry run [V] |
| DEPLOYMENT | First staging deployment | **FAIL / NOT PERFORMED** | No accounts provisioned [L] |
| DEPLOYMENT | Real production environment | N/A (out of scope) | – |
| TESTING | Backend / frontend / E2E / conformance | PASS | 1413 / 69 / 29 / 39 [V] |
| TESTING | Failure drills | PASS locally (12/12) | §31.6 |
| TESTING | Contract tests for every route | PASS | Inventory test [V] |

### 31.8 Known limitations

- **Staging (M12)** was not deployed. None of the §24 steps have run
  against Render, Neon, AuraDB or Vercel.
- **Measurements are local.** No free-tier latency, cold-start or
  Vercel-rewrite measurements exist.
- **CI** has never executed on GitHub, because there is no remote (D10).
- **The in-process sweeper** runs in each instance, serialized by an advisory
  lock. On Render Free the instance sleeps, so work waits for the next wake
  (up to ~1 min cold start plus one sweep interval).
- **Provider cooldown** is per process, not shared.
- **Per-IP rate limiting** is off behind the proxy (D5). Login is protected
  by per-email and global buckets; registration by a global bucket only. It
  is disabled on staging anyway.
- **Uploads via the Next proxy:** over-limit bodies return 500 (early close)
  instead of 413.
- **The PDF parse timeout** abandons the result; it cannot cancel the running
  thread. One parse slot per process prevents pile-up.
- **Eval pre-existing failure:** `eval/runner.py` (extraction evaluation)
  still hits a pre-existing FK failure. It is not part of the deterministic
  CI set.
