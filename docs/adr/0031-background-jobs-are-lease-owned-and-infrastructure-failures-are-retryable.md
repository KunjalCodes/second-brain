# ADR 0031 — Background jobs are lease-owned, infrastructure failures are retryable, and recovery runs in-process

**Status:** Accepted (Phase 11)

## Context

The Phase 11 audit (docs/PHASE11_PLAN.md section 6.2) found that the
PostgreSQL-backed job queue was sound in design but wrong in several details.
Each one could lose or strand canonical memory without anyone noticing:

- A Neo4j outage during extraction recorded candidates as `INVALID` and marked
  the job done, permanently dropping memories.
- Completion writes did not check lease ownership, so a stale worker could
  overwrite a newer owner.
- Exhausted jobs never became terminal.
- The ingestion sweep existed but nothing called it.
- An ingestion retry after a crash could be blocked forever by a foreign key.
- Nothing scheduled the recovery sweeps at all.

## Decision

- **Keep the PostgreSQL queue.** It was measured sufficient: claim p95 was
  37–47 ms with 10 concurrent claimers and no duplicate claims. No Redis and
  no Celery.
- **Infrastructure ≠ semantics.** `INFRASTRUCTURE_ERRORS` (`app/core/errors.py`)
  covers Neo4j/PostgreSQL unavailable or failing, configuration, and storage.
  These always make a job **retryable**, and are never recorded as a verdict
  on a candidate. Only domain validation becomes `INVALID`.
- **Lease ownership, enforced by the database.**
  - Every claim writes a unique lease token into `locked_by`.
  - `mark_done` / `mark_failed` / `defer` are conditional UPDATEs, matching
    `WHERE locked_by = :token AND status = 'in_progress'`.
  - The lease is renewed before a job starts and before every candidate.
  - A worker that lost its lease stops, and its writes match zero rows.
- **Explicit terminal states.**
  - The last failed attempt goes straight to `abandoned`, with its reason.
  - The sweep converts any legacy exhausted `failed` rows and reopens nothing.
  - The target (message or chunk) is marked `failed`, so the document
    finalizes (`FAILED` if no chunk succeeded).
- **Throttling is not failure.** HTTP 429 or provider cooldown defers a job
  without consuming an attempt, until `LLM_THROTTLE_MAX_AGE_SECONDS`.
  `scripts/requeue_jobs.py` recovers jobs abandoned for transient reasons.
- **Resumable ingestion.** Chunks are written atomically, so if they exist, a
  retry reuses them and only ensures their jobs exist (idempotent). It never
  deletes chunks that jobs reference, and it does not need the raw bytes again.
- **In-process sweeper** (`app/workers/sweeper.py`).
  - Started by the lifespan; runs every `SWEEP_INTERVAL_SECONDS`.
  - Guarded by a PostgreSQL advisory lock.
  - Each step is isolated, and failures back off.
  - On graceful shutdown, a pass that doesn't finish within the grace period
    is cancelled, which is safe because of lease ownership.
- **Parser bound.** At most one PDF parser thread exists. A timeout is terminal
  and consistent, and a still-running parse defers the next document without
  consuming an attempt. True cancellation would need a process boundary; this
  is documented, not claimed.

## Consequences

- Every defect has a test that fails on the old behaviour (mutation-checked),
  plus live drills: restart during extraction, and ingestion interrupted.
- At most one duplicate memory per crash remains possible, as documented in
  PHASE2_HARDENING §2.2.
- The sweeper only runs while the process is awake. On a sleeping free-tier
  host, work waits for the next wake-up; `ops.yml` keep-alive bounds that.
