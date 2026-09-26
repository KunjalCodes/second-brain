# ADR 0030 — Free-tier demo topology and its limits

**Status:** Accepted (Phase 10), documentation only. Nothing was deployed.

## Context

Q8 made a public demo optional, and the project is zero-cost. Free tiers
impose sleep, pausing, ephemeral disks and non-commercial terms that change
system behaviour. They must be designed for and written down, not
discovered in production. Terms below were last checked on 2026-09-26 and
**change often**; re-verify before deploying.

## Decision

The demo topology, if deployed:

| Component | Service | Limits that matter |
|---|---|---|
| UI | Vercel Hobby | Non-commercial only; external rewrites time out at 120 s |
| API | Render Free | Sleeps after 15 min idle (~1 min cold start); ephemeral disk |
| PostgreSQL | Neon Free | 0.5 GB; scales to zero |
| Neo4j | AuraDB Free | 200k nodes / 400k relationships; paused after 3 days without writes, deleted 30 days later |
| LLM | Fake, or a free Gemini key | Decided at deploy time (Q9); no paid calls |

Required configuration:

* **UI build:**
  * `BACKEND_ORIGIN=https://<api>` at **build** time (baked into the
    rewrite, ADR 0025);
  * `NEXT_PUBLIC_REGISTRATION_ENABLED` left unset (registration off).
* **API:**
  * `APP_ENV=production`, `AUTH_MODE=session`;
  * `FRONTEND_URL=https://<ui>` (the CSRF Origin allow-list, ADR 0026);
  * `AUTH_REGISTRATION_ENABLED=false`, with accounts created via the operator
    CLI;
  * do **not** put the proxy in `TRUSTED_PROXY_IPS`.

## Consequences

* **Cold start.** The session gate shows "Waking the server…" after 4 s
  instead of an error.
* **Background work** (extraction, ingestion) runs in-process and can be
  interrupted by a spin-down. It needs the existing internal sweep
  endpoints called on a schedule.
* **AuraDB** needs a periodic real write, or it pauses and is later
  deleted.
* **Documents.** Uploaded bytes live on Render's ephemeral disk and are
  lost on restart. Chunks, and therefore provenance, live in PostgreSQL
  and survive.
* **Per-IP rate limits** are a shared bucket behind the proxy (G4 deferred).
  Per-email and per-user limits work.
* **Not production-ready.** No load testing, no backups, no monitoring,
  free-tier availability. This topology is a portfolio demo only.
