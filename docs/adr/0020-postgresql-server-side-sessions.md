# ADR 0020 — Authentication uses server-side opaque sessions in PostgreSQL

**Status:** Accepted (Phase 9). Supersedes [ADR 0005](0005-phase1-identity-placeholder.md).

## Context

Until Phase 9 the requesting user was whatever the client sent in `X-User-Id`
(ADR 0005). Everything downstream of that one input was already isolated: every
Cypher statement filters on `$user_id`, every SQL repository scopes by user, and
every route receives identity from one dependency. Phase 9 had to replace the
input with a verified credential, at zero cost, with no new infrastructure or
dependency, and in a form a future browser UI (Phase 10) can consume.

The options compared were JWT, server-side sessions, API keys, local
passwords, OAuth and a hybrid (docs/PHASE9_PLAN.md section 5).

## Decision

* **Local email + password accounts; server-side opaque sessions in the
  existing PostgreSQL.**
  * The token is `sbs_` + `secrets.token_urlsafe(32)` (256 bits).
  * Only its SHA-256 is stored (`auth_sessions.token_sha256`, unique). A fast
    hash is correct for a high-entropy random secret.
  * Tokens are never logged; logs carry session and user ids.
* **Transport:** an `HttpOnly` cookie for browsers (ADR 0022), and
  `Authorization: Bearer` for scripts, tests and eval. The Bearer header wins
  over the cookie. A presented credential is always verified; an invalid one
  is `401`, never a fall-through to a weaker identity source.
* **Lifetime:** 7 days absolute and 24 hours idle, with the idle window
  sliding on use. `last_used_at` is written at most every 300 s. **No refresh
  tokens and no refresh endpoint**: the sliding window makes them
  unnecessary. Every login creates a new session, so session fixation is
  impossible.
* **Revocation is immediate**, by row update, via:
  * logout (current session);
  * logout-all (every session);
  * password change (every other session);
  * operator disable / reset (every session).
* **Passwords** are hashed with the standard library's `hashlib.scrypt`,
  stored as `scrypt$N$r$p$salt$hash`.
  * The default cost N=2^15, r=8, p=3 is OWASP-listed and measured at
    ~0.2–0.3 s.
  * Production-like environments enforce a floor.
  * Verification uses `hmac.compare_digest`.
  * The hash is re-computed on login when the configured cost changes.
  * An unknown email is verified against a dummy hash, so it costs the same
    work as a wrong password.
* **scrypt runs in a worker thread** (`asyncio.to_thread`). Run inline, one
  login would block the event loop, and so every other request, for its whole
  duration. `hashlib.scrypt` releases the GIL (measured: 4 concurrent hashes
  take about the time of 1). This is pinned by a mutation-verified test.
* **JWT was rejected.** Its one advantage is verification without a database.
  But logout, disable and logout-all all need server state, and PostgreSQL is
  already on every write path. JWT would also add a signing secret, key
  rotation, refresh rotation and a new dependency.
* **OAuth was rejected for now.** It needs public HTTPS callbacks and provider
  registrations; `users.external_auth_id` remains the seam.

## Consequences

* The only route-level change is a 401 for a missing or invalid credential.
  Routes, services, repositories and agent tools still receive a plain
  `user_id: str`.
* Every authenticated request costs one indexed PostgreSQL lookup. Measured
  locally, it adds about 0.3–1.7 ms p50 over the dev-header path
  (docs/PHASE9_PLAN.md section 24).
* PostgreSQL is now required for every authenticated request, including
  Neo4j-only routes. If it is down, requests fail closed with `503`.
* No new dependency and no signing secret to manage.
