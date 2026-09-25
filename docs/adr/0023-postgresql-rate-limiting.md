# ADR 0023 — Rate limiting uses PostgreSQL fixed-window counters, each in its own transaction

**Status:** Accepted (Phase 9)

## Context

Phase 9 needed brute-force protection for login and abuse limits for
expensive endpoints (graph, search, agent turns that may spend on a paid LLM,
message extraction, document ingestion, writes), with no Redis and no new
infrastructure. The per-request Phase 8 budgets bound the work of one request
but not the request rate.

A trap specific to this codebase: `Database.session()` rolls back on any
`AppError`. A counter written in the request's transaction would be erased by
the very `401` or `429` it produced. A failed login would then never count,
leaving brute-force protection silently doing nothing.

## Decision

* **PostgreSQL fixed-window counters** in `rate_limit_counters(bucket_key,
  window_start, count)`, updated by one atomic upsert. **Each hit commits in
  its own transaction** before the request continues. This is pinned by a
  test that four failing logins persist a count of 4.
* **Classes and identities:**

  | Class | Keyed by | Default |
  |---|---|---|
  | Login and password change | client IP **and** SHA-256(email), both counted on every attempt | 10 / 15 min per IP; 5 / 15 min per email |
  | Register | IP | 5 / h |
  | graph | user | 120 / min |
  | search | user | 120 / min |
  | agent | user | 30 / h |
  | message | user | 120 / h |
  | ingest | user | 20 / h |
  | write | user | 120 / min |

  Raw emails are never stored. User classes run after authentication, so
  anonymous floods don't consume a user's quota, and before body validation,
  so invalid requests still count.
* **Client IP** is the direct peer. `X-Forwarded-For` is honoured only from a
  `TRUSTED_PROXY_IPS` peer, taking the right-most non-proxy hop, so a client
  can never choose its own bucket.
* A `429` carries `Retry-After` and `details.limit_class`.
* Old windows are removed by an opportunistic bounded delete on 1% of hits.
  No scheduler is needed.
* **In-process counters were rejected:** they are lost on restart and
  multiplied per worker, which is unacceptable for login limits.
* **Redis was rejected:** it is a new service, excluded by the zero-cost and
  low-complexity constraints.

## Consequences

* Measured locally: one counter hit is 1.46 ms p50 / 2.03 ms p95, adding about
  1.5–4 ms p50 per limited request. That is under the plan's ~5 ms p95
  threshold, so the approved in-process fallback was not needed
  (docs/PHASE9_PLAN.md section 24).
* **Fixed windows allow up to 2× the limit across a boundary.** This is
  accepted.
* **Per-email limits let an attacker lock a known email out of login** for a
  window. This is an accepted trade-off of per-account throttling, documented.
* `RATE_LIMIT_ENABLED=false` disables every limit. The test suite uses that
  and enables limits per test; production must leave it on (it is **not**
  startup-enforced; noted as a limitation).
