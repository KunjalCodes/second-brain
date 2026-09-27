# Deployment — private staging (Phase 11)

This is the runbook for the **private staging** environment (D1). It is **not
production**: free tiers sleep, pause and lose disk contents, and there is no
SLA, monitoring service or managed backup beyond what is listed here.

**Status (2026-09-27):** nothing has been deployed. The provider accounts (Vercel,
Render, Neon, AuraDB) were not available to the implementation. Every step below
that needs an account is marked **[owner]**, and must be executed and recorded in
`docs/DEPLOYMENT_LOG.md` before staging counts as deployed.

## 1. Topology

```text
Browser ─► Vercel Hobby (Next.js; /api/* rewrite fixed at build time)
             └─► Render Free web service (FastAPI + in-process sweeper)
                   ├─► Neon Free (PostgreSQL + pgvector)
                   ├─► AuraDB Free (Neo4j)
                   └─► /tmp/second-brain/documents (ephemeral; bytes deleted after ingestion)
GitHub Actions (needs a remote, D10): ops.yml keep-alive + encrypted nightly backups
```

## 2. Free-tier facts that shape the design

Checked against official pages on 2026-09-26. They change often, so re-check before deploying.

| Service | Fact | Consequence |
|---|---|---|
| Vercel Hobby | Non-commercial only; external rewrite timeout 120 s; function bodies capped at 4.5 MB. The limit for pure external rewrites is not documented | The upload path must be measured on the real deployment (§5, step 10) |
| Render Free | Sleeps after 15 min idle (~1 min cold start); ephemeral disk; 750 h/month; no free cron | Bytes are transient (D3); the sweeper runs in-process; keep-alive comes from `ops.yml` |
| Neon Free | 0.5 GB, 100 CU-h/month, scale-to-zero, pgvector available, 6 h restore window | Our own encrypted dumps are the real backup (§6) |
| AuraDB Free | 200k nodes / 400k relationships; pauses after 72 h inactivity and is **deleted 30 days after pausing**; manual snapshots only; **Cypher 5** by default | Keep-alive plus exports. Local development runs `CYPHER_25`, so run the compatibility check (§5, step 4) |

## 3. Backend configuration (Render)

`render.yaml` sets the non-secret staging values. Startup **refuses** unsafe
combinations (`backend/app/core/config.py::_check_deployment_requirements`), so a
misconfiguration fails the deploy instead of degrading silently.

| Variable | Staging value | Why |
|---|---|---|
| `APP_ENV` | `staging` | Production-like: turns on every startup guard, HSTS, JSON logs, hidden docs |
| `AUTH_MODE` | `session` | Required |
| `AUTH_REGISTRATION_ENABLED` | `false` | Accounts come from the operator CLI |
| `LLM_PROVIDER` / `EMBEDDING_PROVIDER` | `fake` | D7. The offline client answers honestly ("AI answers are turned off") |
| `ALLOW_FAKE_AI` | `true` | The explicit opt-in the guard requires for fake AI |
| `EXPOSE_DOCS` | `false` | D8 |
| `RATE_LIMIT_PER_IP` | `false` | D5: the backend cannot see real client IPs behind Vercel and Render. Global login/register ceilings replace per-IP buckets; per-email and per-user limits stay |
| `LOG_FORMAT` | `json` | Correlated structured logs |
| `DOCUMENT_STORAGE_DIR` | `/tmp/second-brain/documents` | Must be absolute; ephemeral by design |
| `DOCUMENT_DELETE_BYTES_AFTER_INGESTION` | `true` | Bytes are not needed after chunking (D3) |
| `DATABASE_URL` | secret | Neon connection string (`postgresql+asyncpg://…`) |
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | secret | Aura |
| `INTERNAL_API_TOKEN` | generated (≥ 32 chars, enforced) | `/internal/*` (sweep, backfill, metrics) |
| `FRONTEND_URL` | secret-ish | The Vercel origin, `https://…` (CSRF Origin allow-list) |
| `TRUSTED_PROXY_IPS` | **unset** | Never trust the proxy's `X-Forwarded-For` (ADR 0026) |

Real AI (optional, owner's decision): set `LLM_PROVIDER=gemini`, `LLM_MODEL`,
`GEMINI_API_KEY`, and remove `ALLOW_FAKE_AI`. Expect HTTP 429 on free keys. Jobs
wait out throttling without spending attempts, for up to
`LLM_THROTTLE_MAX_AGE_SECONDS`.

## 4. Frontend configuration (Vercel)

| Variable | Value | Notes |
|---|---|---|
| `BACKEND_ORIGIN` | `https://<render-service>.onrender.com` | **Build-time**. The build fails if it is missing or not https |
| `MAX_DOCUMENT_SIZE_BYTES` | same as the backend (default 20 MiB) | Sizes the Next.js proxy's body buffer (§5, step 10) |
| `NEXT_PUBLIC_REGISTRATION_ENABLED` | unset | Registration stays off |

## 5. First deployment procedure

"The server starts" is **not** success. Record each result in `docs/DEPLOYMENT_LOG.md`.

1. **[owner] Provision.** Neon project (region near Render), AuraDB Free instance,
   Render web service from `render.yaml`, Vercel project with root `frontend/`.
   *Pass:* all four exist.
2. **[owner] Secrets.** Enter the §3 and §4 secrets in the dashboards, never in git.
   *Pass:* a Render deploy boots. A wrong value fails the boot with a list of
   problems.
3. **Migrations** (from the operator machine, before the first backend deploy):
   `cd backend && DATABASE_URL=<neon> uv run alembic upgrade head && uv run alembic check`.
   *Pass:* `alembic current` is `320f6d898a92 (head)`; `alembic check` reports no
   new operations; `memory_embeddings_hnsw_cosine` exists (`\di` in psql).
4. **Graph schema + Cypher compatibility:**
   `NEO4J_URI=<aura> … uv run python ../scripts/init_graph.py`. Then run the graph
   conformance evaluation against Aura:
   `LLM_PROVIDER=fake uv run python eval/graph_api_runner.py`.
   *Pass:* schema applied; **39/39** tasks. This is the check that Aura's Cypher 5
   default accepts every query the app uses (local runs `CYPHER_25`).
5. **Backend health:** `curl https://<render>/health/ready`.
   *Pass:* `{"status":"ready","checks":{"neo4j":"ok","postgresql":"ok","migrations":"ok"}}`.
6. **Operator account:**
   `uv run python ../scripts/manage_users.py create --email <you>` (prompted password).
   *Pass:* account created.
7. **[owner] Frontend deploy** with `BACKEND_ORIGIN` set. *Pass:* the build succeeds.
8. **Authentication:** log in through the UI.
   *Pass:* DevTools shows `sb_session` as HttpOnly + Secure + SameSite=Lax;
   `/api/auth/me` returns 200 with `Cache-Control: no-store`; the response carries
   HSTS.
9. **Graph:** `uv run python ../scripts/seed_demo.py --user-id <your user_id>`.
   *Pass:* overview and neighbourhood render; the EasyOCR memory shows as
   superseded.
10. **Upload path — measure on the real deployment (D4):** upload 1 MiB, 4 MiB,
    20 MiB (the limit) and 20 MiB + 1 byte through the Vercel URL.
    *Pass:* the first three return 201; the last is refused (422/413).
    **Measured locally** through `next start` (not Vercel):
    - with the default proxy buffer, 20 MiB was silently truncated at 10 MB and
      failed with a 500 after 30 s;
    - with `experimental.proxyClientMaxBodySize` set (now in `next.config.ts`),
      1/4/20 MiB return 201 and 20 MiB + 1 byte returns 422 `file_too_large`.

    If Vercel caps the rewrite lower, lower `MAX_DOCUMENT_SIZE_BYTES` on **both**
    sides to the largest size measured to succeed. Never pick a number without
    measuring.
11. **Extraction:** send a message in Ask.
    *Pass:* within one sweep interval (60 s) the message's extraction job is
    `done`. With fake AI there are zero candidates by design.
12. **Search:** `POST /api/search` returns 200, with results after step 9.
13. **Agent:** one Ask turn returns 201 with the offline answer (or a real answer
    if real AI is configured).
14. **Backup:** trigger `ops.yml` → *backup* (needs the remote, D10, plus the
    `STAGING_*` and `BACKUP_PASSPHRASE` secrets). *Pass:* an encrypted artifact
    exists.
15. **Restore drill** from that artifact (§6.3). *Pass:* `verify` prints `"ok": true`.
16. **Rollback:** redeploy the previous Render deploy from the dashboard.
    *Pass:* readiness returns 200. Migrations are forward-only; a bad migration is
    fixed forward.
17. **Failure drills** (subset, §7) on staging, then update the checklist in
    `docs/PHASE11_PLAN.md`.

## 6. Backups (D12)

### 6.1 What is backed up

`scripts/backup.py create` produces, in one directory:
- the PostgreSQL dump (`pg_dump -Fc`, everything);
- the full Neo4j graph as typed JSONL (plain Cypher, no APOC);
- any document bytes still on disk;
- a manifest of counts and SHA-256 checksums, written last.

`--encrypt` encrypts with `openssl enc -aes-256-cbc -pbkdf2` using
`BACKUP_PASSPHRASE`, which is read from the environment only.

`pg_dump` must be the server's major version or newer. Point `PG_BIN_DIR` at it,
e.g. `/opt/homebrew/opt/postgresql@17/bin`.

### 6.2 Schedule and retention

`ops.yml` runs daily at 03:17 UTC and keeps artifacts for 7 days. Download them
periodically to storage you control. GitHub artifacts are a convenience copy, not
off-site storage.

### 6.3 Restore (and the drill)

```bash
createdb restored_db   # an EMPTY PostgreSQL database
# an EMPTY Neo4j (a fresh Aura instance, or a scratch local instance)
export BACKUP_PASSPHRASE=… TARGET_NEO4J_PASSWORD=… PG_BIN_DIR=…
uv run python ../scripts/backup.py restore --from <dir> \
    --target-database-url postgresql://…/restored_db --target-neo4j-uri bolt://…
NEO4J_URI=… uv run python ../scripts/init_graph.py
uv run python ../scripts/backup.py verify --from <dir> --target-database-url … --target-neo4j-uri …
```

Restore refuses non-empty targets. `verify` compares every table count, every
label and relationship count, and the memory temporal fields against the
manifest, then runs `scripts/check_consistency.py`, which must report 0 orphans.

**Local drill performed 2026-09-27** (full test database + local Neo4j, with a
scratch Neo4j instance on :7688 and a scratch PostgreSQL database as targets):

| Step | Result |
|---|---|
| Encrypted backup | 2.5 s |
| Restore + schema | 5.0 s |
| `verify` | `ok: true`, 0 orphans |

A backend booted on the restored stores then passed readiness and served the
restored data:
- login with a restored account;
- the superseded memory, with `valid_until` intact;
- "true now" vs "as of March";
- review queues, the document and its chunks;
- hybrid search using the restored embeddings;
- the graph neighbourhood.

These times were measured on a small dataset. They are not an RTO for larger
data.

### 6.4 What a restore cannot give back

- Anything written after the backup (RPO: up to 24 h with the nightly job).
- Raw bytes of documents already ingested (deleted by design in staging). Their
  chunks, provenance and memories *are* restored.
- Sessions older than the backup (users log in again).
- PostgreSQL and Neo4j snapshots are seconds apart, not atomic. `verify`'s
  consistency check reports any cross-store orphans a write in between produced.

### 6.5 Guarantees that do NOT exist on the free tier

- Neon keeps only 6 h of history.
- Aura Free has no scheduled backups, and a paused instance is deleted after
  30 days.
- Render's disk is wiped on every restart.
- Without the remote repository, no scheduled backup runs at all.

## 7. Failure drills

Drills 1, 2, 5, 6, 8, 9 and 10 were run live against a real backend process on
2026-09-27 (`scripts/failure_drills.py`, test database only). Drills 3, 4, 7 and
12 are fault-injection integration tests, and drill 11 is the OpenAPI drift
check. The restore drill (§6.3) is recorded separately. Results are recorded in the Phase 11 report
(`docs/PHASE11_PLAN.md` §31). On staging, repeat 1, 3, 5, 8 and 9 after
deployment.

## 8. Operating notes

- **Metrics:** `curl -H "X-Internal-Token: …" https://<render>/internal/metrics`
  returns queue depth, oldest pending job, abandoned jobs in the last 24 h,
  stuck documents, error counts, LLM provider health, and the sweeper's last pass.
- **Requeue after a provider outage:**
  `uv run python ../scripts/requeue_jobs.py --reason llm_unavailable` (a dry
  run; add `--apply`). Only transient reasons can be requeued; targets are
  reopened, and the sweeper retries the jobs.
- **Cold start:** the first request after idle wakes Render (~1 min), then Neon
  (scale-to-zero). The UI shows "Waking the server…".

## 9. Migration policy

- **Forward-only in deployed environments.** Rollback means redeploying the
  previous build against the same schema; a bad migration is fixed with a new
  migration. `alembic downgrade` is never part of a deployment or rollback.
- **The downgrades are destructive.** They exist for local development only.
  Each one drops what its upgrade created, **including the rows**:

  | Revision | Downgrade drops |
  |---|---|
  | `320f6d898a92` (Phase 9) | `user_accounts`, `auth_sessions`, `rate_limit_counters` |
  | `ec358e32d566` (Phase 7) | `evaluation_runs`, `evaluation_task_results` |
  | `536b236d2a78` (Phase 6) | `documents`, `document_chunks`, `ingestion_jobs`, chunk columns on `extraction_jobs` / `extraction_candidates` |
  | `5fd9c1b13f46` (Phase 4) | `review_audit_log`, `memory_review_candidates`, a column of `entity_merge_candidates` |
  | `6202eeca7d8d` (Phase 3) | `memory_embeddings` (and the HNSW index) |
  | `7fcb56a35aa9` (Phase 2) | `messages`, `extraction_jobs`, `extraction_candidates` |

  Neo4j is not migrated by Alembic, so a PostgreSQL downgrade also leaves the
  graph out of step with it.
- **Before any upgrade** on staging, take a backup (§6). Readiness reports
  `migrations=mismatch` (503) until the schema matches the code's head.
- **Autogenerate** ignores the raw-SQL HNSW index
  (`RAW_SQL_MANAGED_OBJECTS` in `migrations/env.py`), so `alembic check` stays
  clean and never proposes dropping it. CI runs `alembic heads` (single head) and
  `alembic check`.
