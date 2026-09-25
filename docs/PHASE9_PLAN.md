# Phase 9 — Authentication & Productization Foundation

**Status:** IMPLEMENTED (approved with decisions Q1–Q10; see section 24 for what was built, measured and deferred).
**Baseline:** commit `e028188` (Phase 8 complete), 942 tests passing.
**Constraints:**
- zero cost;
- no Docker, no Redis, no paid or cloud auth provider;
- local Neo4j Community and local PostgreSQL;
- no new dependency unless justified in section 20.

---

## 1. Executive Summary

Today the backend has **no authentication**. The requesting user is whatever
the client puts in the `X-User-Id` header, falling back to `DEV_USER_ID`
(ADR 0005). Everything *downstream* of that one function is already built
for real multi-user isolation:

- every Cypher statement filters on `$user_id`, audited statically, with
  auto-discovery since Phase 8;
- every SQL repository scopes by `user_id` through `UserScopedRepository`,
  also audited statically;
- every user-facing route takes identity from one dependency, `CurrentUser`;
- agent tool schemas cannot carry a `user_id`;
- document bytes live under per-user directories.

Phase 9 therefore does not need to redesign the memory system. It needs to
replace **one function's input** — a client-asserted header — with a
server-verified credential, and to add the controls a real credential
requires.

**Recommendation** (section 6):

| Area | Choice |
|---|---|
| Credential | Server-side opaque sessions stored (hashed) in the existing PostgreSQL; local email + password accounts |
| Password hashing | Stdlib `hashlib.scrypt` |
| Token transport, browsers | `HttpOnly` cookie |
| Token transport, other clients | `Authorization: Bearer` (tests, scripts, eval) |
| Rate limiting | PostgreSQL-backed fixed-window counters |
| New dependencies | **None** |

No JWT, no refresh tokens, no OAuth, no Redis. Memory ownership stays keyed
by the existing `users.id` string. Neo4j data is not touched.

Phase 9 must also close three concrete gaps found during inspection:

1. **CORS.** Credentialed requests are currently allowed from *any*
   `https://*.vercel.app` origin, not only this project's (section 3, F7).
2. **Lazy user creation.** Any unseen `X-User-Id` silently creates a user
   (F4).
3. **Rolled-back counters.** The request DB session rolls back on every
   `AppError`, so a rate-limit counter written inside it would vanish on
   every failed login (F12).

---

## 2. Current Identity Architecture

```
HTTP request
  └─ X-User-Id header  (or DEV_USER_ID fallback)          ← client-asserted, NOT verified
       └─ app.api.deps.get_current_user_id()               ← the ONE resolution point
            └─ CurrentUser = Annotated[str, Depends(...)]
                 └─ route handler passes user_id=... explicitly
                      ├─ services (MemoryService, RetrievalService, GraphExplorationService,
                      │   ConversationService, DocumentService, ProvenanceService,
                      │   HybridRetrievalService, AgentService, Review*Service)
                      ├─ Neo4j repositories: every statement filters $user_id
                      │   (tests/unit/test_cypher_safety.py, auto-discovered)
                      ├─ SQL repositories: UserScopedRepository adds WHERE user_id = :u
                      │   (tests/unit/test_sql_safety.py AST audit)
                      ├─ Document storage: <root>/<user_id>/<uuid>, user_id charset-validated
                      └─ Agent tools: user_id is a Python parameter, never a tool argument
```

- **PostgreSQL.** `users` (`app/db/models.py::User`) has `id VARCHAR(128)` PK,
  `external_auth_id VARCHAR(255) UNIQUE NULL` (an unused seam) and
  `created_at`. It holds no credentials, by design. Every user-owned table
  has `user_id` with an FK to `users.id`.
- **Neo4j.** There is no user node. Ownership is the `user_id` property on
  every `Memory`, `Entity`, `Source` and every relationship that carries it.
- **Production guard.** `Settings._check_production_requirements` refuses to
  start when `APP_ENV=production` and `ALLOW_USER_ID_HEADER=true`. With the
  header disabled, `get_current_user_id` raises `ConfigurationError`, so
  production currently cannot serve any user request at all.

---

## 3. Repository Findings

Each finding was inspected directly; file references are relative to
`backend/` unless stated.

| # | Finding | Evidence | Phase 9 impact |
|---|---|---|---|
| F1 | Identity is resolved in exactly one place. It takes `X-User-Id` (strip, non-empty, ≤128 chars) and falls back to `DEV_USER_ID="dev-user"`. | `app/api/deps.py::get_current_user_id`, `MAX_USER_ID_LENGTH=128` | The replacement point. ADR 0005's "replace this function and nothing else" still holds for route code. |
| F2 | **Every** user-facing route depends on `CurrentUser`: memories 6, entities 7, graph 4, search 1, conversations 5, agent 1, documents 4, extraction 2, review 11. Public: `/health`, `/health/ready`. Internal: `/internal/extraction/sweep`, `/internal/embeddings/backfill`, `/internal/review/reconcile`. | route-by-route count of decorators vs `CurrentUser` / `InternalTokenDep` | No route needs rewriting. A meta-test can enforce "every route is authenticated, public, or internal". |
| F3 | No request schema carries `user_id`; agent tool arguments are `extra="forbid"` and have no `user_id`. `user_id` appears only in internal records (`app/models/extraction.py`). | `app/schemas/*`, `app/models/agent.py` docstring | Body and tool-argument identity injection is already structurally impossible. Phase 9 adds tests that pin it. |
| F4 | **Lazy user provisioning.** `ConversationService` and `DocumentService` call `UserRepository.ensure_exists(user_id)` (`INSERT ... ON CONFLICT DO NOTHING`). `POST /memories` writes to Neo4j only and never creates a `users` row. | `app/services/conversation_service.py:60`, `document_service.py:81` | Today any header value becomes a user. After Phase 9, users exist only through registration or the admin CLI, and `ensure_exists` becomes a harmless no-op for authenticated users. Neo4j-only "users" (e.g. `demo-user` from `scripts/seed_demo.py`) have no PG row. |
| F5 | `users.id` values are free-form strings, not UUIDs: `dev-user`, `demo-user`, `itest-<uuid>`, `eval-graph-...`, `bench-...`. | `scripts/seed_demo.py:43`, tests, eval | The ownership id must stay a string key. New accounts can use UUID4 strings; legacy ids stay valid. |
| F6 | `UnauthorizedError` (401) exists but is scoped to internal-token checks. `ConfigurationError` fires when the header mode is off. Cross-user access returns **404**, never 403 (ADR 0005 rule 3). | `app/core/errors.py`, `app/api/deps.py` | Reuse 401 for missing or invalid credentials; keep 404 for cross-user. |
| F7 | **CORS:** `allow_credentials=True`, `allow_headers=["*"]`, explicit origins from `FRONTEND_URL` + `CORS_ORIGINS`, **plus** `allow_origin_regex=r"https://.*\.vercel\.app"` when `CORS_ALLOW_VERCEL_PREVIEWS=true` (the default). With cookie credentials, *any* site on vercel.app — not just this project's previews — could make credentialed requests and read the responses. Harmless today (no credentials exist); a real vulnerability once cookies do. | `app/main.py:126-137`, `app/core/config.py:50-55, 293-308`; tests `tests/unit/test_api.py:420-444` assert the preview behaviour | Must change (section 13). The existing CORS tests change with it. |
| F8 | `.env.example` already declares `AUTH_SECRET=` but no code reads it. There is also `STORAGE_SECRET_KEY` (unused). `backend/.env` is git-ignored and untracked. | `.env.example:183, 210` | Decide whether a secret is needed at all; opaque sessions need none (section 13). |
| F9 | Document storage isolates by path: `<root>/<user_id>/<uuid>`, `user_id` validated against a strict charset, `storage_key` must match a UUID pattern. | `app/storage/local.py` | Unchanged. The ownership id reaching it becomes server-derived. |
| F10 | Agent: `AgentService.run_turn(user_id=...)` threads the id into `ToolDependencies`; tools receive `user_id` as a keyword argument; logs include `user_id` and tool args. | `app/services/agent_service.py`, `app/agent/tools.py` | Unchanged. Tests must prove that neither LLM output nor tool arguments can switch the user. |
| F11 | Tests: 59 `X-User-Id` usages in 6 files (`tests/unit/test_api.py`, `test_config.py`, `tests/integration/test_{graph_api,agent_api,conversations_api,documents_api}.py`). The autouse fixture sets `ALLOW_USER_ID_HEADER=true`; `DEV_USER_ID=test-user`. Cross-user tests exist in 16 files. Most integration tests call services directly with a `user_id`, bypassing HTTP. | `tests/conftest.py`, `tests/integration/conftest.py` | The dev-header mode must keep working under test (section 12), plus a new session-mode suite. |
| F12 | **Transaction semantics:** `Database.session()` commits on success and **rolls back on any `AppError`**. The request-scoped `get_db_session` uses it. Many Neo4j-only routes (memories, entities, graph) currently open no PG session at all. | `app/db/engine.py:127-146`, `app/api/deps.py` | Any auth or rate-limit write must run in its **own** short transaction, or it is rolled back by the 401 or 429 it produces. Every authenticated request now needs one PG read (session lookup), including Neo4j-only routes. |
| F13 | Evaluation harnesses call services directly with synthetic `user_id`s. Only `eval/graph_api_benchmark.py` issues HTTP requests with `X-User-Id`. Evaluation tables (`evaluation_runs`, `evaluation_task_results`) are not user-scoped and have **no HTTP endpoint** (CLI `eval/report.py` only). | `eval/*.py`, `app/repositories/evaluation_repository.py` (SQL-audit exempt) | Harnesses keep working unchanged at the service layer. Evaluation data stays CLI-only, not reachable by users. |
| F14 | Frontend: not implemented. `frontend/README.md` says "Until authentication lands in Phase 8 the backend reads `X-User-Id`" (stale). `frontend/.env.example` points `NEXT_PUBLIC_API_URL` at a separate backend host (`*.up.railway.app`), i.e. a **cross-site** topology. | `frontend/README.md`, `frontend/.env.example` | Cross-site cookies are increasingly blocked by browsers. The Phase 10 topology must be same-site (section 18). The stale README is updated during implementation, not now. |
| F15 | The internal endpoints use a constant-time `X-Internal-Token` check that fails closed when unset. | `app/api/deps.py::require_internal_token` | Unchanged. Operator credential, not user auth. |
| F16 | No rate limiting exists anywhere. Existing bounds are per-request only (graph budgets, `SEARCH_MAX_LIMIT`, document size/pages/chunks, agent tool-call and turn timeouts). | `app/core/config.py` | Add per-identity request-rate limits on top. Never weaken per-request budgets. |
| F17 | Dependencies: no JWT, argon2, bcrypt or passlib. `cryptography` is present only transitively (via `google-auth`). Stdlib `hashlib.scrypt` works here (OpenSSL 3.5.5). | `pyproject.toml`, `uv pip list` | Password hashing and token generation are possible with zero new dependencies (section 20). |
| F18 | Uvicorn is run directly (`uvicorn app.main:app`). No proxy-header handling is configured, so `request.client.host` is the direct peer. | `README.md:418`; no `ProxyHeadersMiddleware` / `forwarded` in `app/` | IP-based limits are correct locally and behind a proxy only with explicit trust configuration (section 11). |

---

## 4. Threat Model

The project scope: a local-first, zero-cost system, deployable later to a
single small host. Each user holds their own private memories. There are no
teams, no admins in the product, and no payments.

| Threat | Class | Mitigation / reason |
|---|---|---|
| Unauthenticated access to user APIs | **Must** | Every non-public route requires a valid session (section 8), enforced by a route meta-test. |
| User A reads User B's memories/entities/graph/documents/chunks/conversations/sources/provenance | **Must** | Already enforced by `$user_id` / `UserScopedRepository` given a trustworthy id. Phase 9 makes the id trustworthy and re-proves isolation through authenticated HTTP. |
| Guessing another user's ids | **Must** | Ids are UUID4s, and ownership is checked in the query itself, so guessing gives 404 (unchanged). |
| Forged `X-User-Id` header | **Must** | In session mode the header is ignored entirely. Dev-header mode is impossible in production (hard fail). |
| Forged `user_id` in body/query/path/tool arguments/LLM output/document metadata | **Must** | No schema has the field (F3); tool arguments are `extra="forbid"`. Tests pin both. Extraction proposals from documents cannot name an owner. |
| Development identity surviving into production | **Must** | Existing guard, extended: production requires session mode, `Secure` cookies and an explicit CORS list (section 12). |
| Brute-force login / credential stuffing | **Must** | Per-IP and per-email login limits in PostgreSQL (section 11); a deliberately slow password hash; the same work for unknown emails. |
| Session theft (XSS, logs, disk) | **Must** | `HttpOnly` cookie. Only the token's SHA-256 is stored, never logged. Logout, logout-all and password change revoke sessions. |
| Replay of expired or revoked credentials | **Must** | Every request checks `expires_at`, `revoked_at`, idle timeout and account status server-side. |
| CSRF (the cookie transport makes this real) | **Must** | `SameSite=Lax` plus a strict Origin check on unsafe methods for cookie-authenticated requests. Bearer requests are CSRF-immune. |
| Overly permissive CORS (F7) | **Must** | Remove the wildcard preview regex from credentialed CORS by default; production requires explicit origins. |
| Excessive graph / agent / search / ingestion requests | **Should** | Per-user rate limits per endpoint class. Per-request budgets remain the hard ceiling. |
| Excessive LLM spend via agent turns / message extraction | **Should** | Per-user hourly limits on LLM-triggering endpoints (costs only apply if a paid provider is configured). |
| Error-message leakage | **Should** | A uniform 401 body; login failures never reveal whether the email exists; cross-user access stays 404. Secrets are never logged (section 13). |
| Email enumeration via registration (409 on duplicate) | **Should** (accepted) | Mitigated only by registration rate limits. A non-enumerating flow needs email delivery, which is deferred (section 21, Q7). |
| Prompt injection trying to change ownership | **Should** (already structural) | The LLM cannot express a user id anywhere it is read. Retrieved content stays data (ADR 0007). Add explicit tests. |
| Secrets committed to git | **Should** | Opaque sessions need no signing secret. `.env` stays ignored; add a test asserting `.env.example` contains no non-empty secret values. |
| Account takeover via password reset | **Deferred** | No reset-by-email exists. Reset is an operator CLI only (section 16). |
| Distributed/botnet abuse, network DDoS | **Deferred** | Out of scope for a zero-cost single-host deployment; belongs to the hosting edge. |
| Multi-factor authentication, device management | **Deferred** | Not justified for this project's risk level. |
| Timing attacks on token lookup | **Deferred** (low) | The lookup is by SHA-256 of a 256-bit random token through a unique index; a timing oracle on a hash prefix gives no useful advantage. |
| Malicious insider with DB access | **Deferred** | Out of scope. At-rest encryption is a hosting concern. |

---

## 5. Authentication Options

| | A. JWT (stateless) | B. Server-side sessions | C. API keys | D. Local email/password | E. OAuth/social | F. Hybrid |
|---|---|---|---|---|---|---|
| What it is | A signed, self-contained access token plus a refresh token | An opaque random token; state in PostgreSQL | Long-lived static bearer secrets | *How a user proves identity* (pairs with A, B or C) | A delegated identity provider | B + D, with the token accepted as cookie **or** Bearer |
| Complexity | Medium-high: signing, key rotation, refresh rotation, a revocation list | Low: one table, one indexed lookup | Very low | Medium: hashing, limits, the account table | High: redirects, state/PKCE, provider apps | Low-medium |
| Dependencies | PyJWT (or hand-rolled HMAC — never) | **none** | none | **none** (stdlib scrypt) or argon2-cffi | httpx exists, but provider registration/config, and it requires public HTTPS callbacks | **none** |
| Revocation | Hard: needs server state anyway (denylist), which removes the stateless benefit | Immediate (a row update) | Immediate | n/a | Provider-dependent | Immediate |
| Refresh tokens | Required (short access tokens) | Not needed: sliding idle expiry | n/a | n/a | Provider tokens | Not needed |
| CSRF | None if kept in JS memory; present if put in a cookie | Present with cookies (mitigated by SameSite + Origin) | None (header) | n/a | State parameter | Cookie path mitigated; Bearer path none |
| XSS | Token in JS → stealable | `HttpOnly` → not readable | Key in JS → stealable | n/a | Same as the chosen session | Browser uses `HttpOnly` |
| Browser fit (Phase 10) | Works, but token storage is the weak point | Best: the browser handles cookies | Poor (users paste keys) | Required for B/F | Good UX, poor zero-cost/local fit | Best |
| Local development | OK | Good (`Secure` off only in dev) | Good | Good | Poor (callback URLs, provider consoles) | Good |
| Zero-cost | Yes | Yes | Yes | Yes | Mostly free tiers, but an external dependency and account per provider | Yes |
| Scale/fit | Multi-instance without shared state — irrelevant here: one PostgreSQL is already on every write path | One PG lookup per request, measured in implementation | — | — | — | Same as B |
| Migration from `X-User-Id` | Medium | Low: `get_current_user_id` looks up a session instead of reading a header | Low | Low | Medium | Low |

**Why not JWT.** Its one real advantage is verification without a database.
This system cannot use that: logout, disabled accounts and "revoke all
sessions" all need server state, and PostgreSQL is already a hard dependency
on every write path. JWT would add a signing secret, key rotation, refresh
rotation and a new dependency, only to rebuild server-side revocation
afterwards.

**Why not OAuth now.** It needs public HTTPS callback URLs and provider app
registrations, which conflicts with local-first development. The
`users.external_auth_id` seam keeps it addable later without touching
ownership.

**Why not API keys for users.** They are poor for browsers and long-lived by
nature. They remain useful for automation; Bearer session tokens cover
scripts, tests and eval in Phase 9, so no separate key system is needed.

---

## 6. Recommended Authentication Architecture

**F. Hybrid = B + D.** Local email/password accounts; on login, the server
issues an opaque session token stored hashed in PostgreSQL. The browser
receives it in an `HttpOnly` cookie. Non-browser clients receive the same
token in the login response body and send `Authorization: Bearer <token>`.

```
POST /auth/login {email, password}
  └─ rate limit (IP, email)                   [own transaction — survives the 401]
  └─ look up account by normalised email
  └─ scrypt-verify (dummy verify when unknown → same work)
  └─ status must be active
  └─ token = secrets.token_urlsafe(32)        (256-bit)
  └─ INSERT auth_sessions(id, user_id, token_sha256, expires_at, last_used_at …)
  └─ Set-Cookie: sb_session=<token>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age
  └─ body: {user, session: {expires_at}, token?}   (token only if the client asked for it; section 9)

Any authenticated request
  get_current_user_id():
    credential = Authorization: Bearer … else cookie sb_session else none
    if credential:
        row = SELECT … FROM auth_sessions JOIN user_accounts
              WHERE token_sha256 = sha256(credential)
        valid iff: not revoked, now < expires_at, now < last_used_at + idle,
                   account.status = 'active'
        cookie + unsafe method → Origin check (section 13)
        touch last_used_at at most once per AUTH_SESSION_TOUCH_SECONDS   [own transaction]
        return row.user_id                       ← the ownership id; routes unchanged
    elif AUTH_MODE == "dev_header":              (never in production)
        return X-User-Id or DEV_USER_ID          (today's behaviour, byte-for-byte)
    else:
        raise 401 authentication_required
```

**Why this fits the repository.**

- It changes only `get_current_user_id` and adds an `/auth` router. Every
  route, service, repository, Cypher statement and agent tool keeps
  receiving a `user_id: str` exactly as today (F1, F2).
- It adds no new infrastructure or dependency (F17). PostgreSQL is already
  deployed and used for everything auth needs.
- Revocation is immediate. Disabling an account kills every session on the
  next request.
- The cookie transport suits the Phase 10 browser UI; the Bearer transport
  keeps tests, scripts and eval simple and CSRF-free.

**Decided here so the implementer doesn't have to:**

| Aspect | Decision |
|---|---|
| Token | `secrets.token_urlsafe(32)`, optionally prefixed `sbs_` for secret scanners. The server stores `sha256(token)` hex (a fast hash is correct for a 256-bit random secret). |
| Password hash | `hashlib.scrypt`, stored as `scrypt$N$r$p$salt_b64$hash_b64`. 16-byte random salt, 32-byte key. Parameters come from settings and are **measured at implementation time**: target ~100–300 ms per verify on the dev machine, not below OWASP's scrypt floor (N=2^17, r=8, p=1, 128 MiB; or an equivalent documented OWASP alternative such as N=2^16/r=8/p=2 or N=2^15/r=8/p=3). Pass `maxmem` explicitly (the OpenSSL default of 32 MiB rejects N=2^17). Verify with `hmac.compare_digest`. Re-hash on login when stored parameters are weaker than the configured ones. |
| Password policy | Length 12–128 characters (NIST SP 800-63B style); no composition rules; reject if equal to the email. No breached-password API (network, not zero-cost-offline). |
| Email | Trim, lowercase, then a basic syntax check (`local@domain`, ≤254 chars). Unique on the normalised value. No verification email (deferred). |
| Session lifetime | Absolute `AUTH_SESSION_TTL_HOURS=168` (7 days). Idle `AUTH_SESSION_IDLE_HOURS=24`. `last_used_at` touched at most every `AUTH_SESSION_TOUCH_SECONDS=300` to bound writes. No refresh endpoint: the idle window slides. |
| Concurrency | Several sessions per user allowed (multiple devices). Login always creates a **new** session, so session fixation is impossible. |
| Password change | Revokes **all other** sessions of the user; the current session stays. |

---

## 7. User Identity Model

The core separation:

```
AUTH IDENTITY (who proves it)                 MEMORY OWNERSHIP IDENTITY (whose data)
───────────────────────────────               ───────────────────────────────────────
user_accounts.email      (login handle)       users.id  ← the ONE ownership key
user_accounts.password_hash                     = PostgreSQL user_id FKs
auth_sessions.*          (credentials)          = Neo4j user_id property
users.external_auth_id   (future OAuth seam)    = storage directory name
```

- **Canonical user id** = `users.id`, a string of at most 128 characters.
  Unchanged type and semantics.
  - New accounts get `str(uuid4())`.
  - Legacy ids (`dev-user`, `demo-user`) stay valid; an operator attaches
    credentials to them with the CLI (section 12). **No Neo4j rewrite, no
    data migration.**
- **Authentication identifier** = normalised email in `user_accounts`. Email
  is *not* the ownership key, so changing it later never touches memory data.
  Phase 9 exposes no email-change endpoint; the model supports one.
- **The server always chooses `user_id`**: at registration (UUID4) or in the
  operator CLI. No client-facing request ever carries it.
- **Is `users` sufficient?** It is, as the ownership row. Auth data goes in
  separate tables, so the Phase 1 promise that `users` holds no credentials
  stays true. `users` itself gets **no schema change**.
- **Account status:** `active | disabled`. There is no `deleted` state:
  deleting a user's memory graph is a separate, deferred data-lifecycle
  feature.
- **Provisioning:** in session mode a `users` row always exists before any
  write, created by registration or the CLI. `ensure_exists` stays as a
  harmless idempotent no-op. In dev-header mode it keeps today's lazy
  behaviour.

---

## 8. Authorization Model

**Invariant:** the `user_id` passed to any service is exactly the
`users.id` bound to the verified session, or, only in dev-header mode, the
dev header value. It is never taken from:

- the request body, query or path;
- tool arguments or LLM output;
- document metadata;
- any client header other than the credential.

**Model:** single-owner isolation. There are no roles, sharing or admins in
Phase 9 (RBAC is a non-goal). Authorization = "the record's `user_id` equals
the caller's `user_id`", enforced inside every query (not by a pre-check),
exactly as today.

**Enforcement points, all existing and unchanged:**

| Layer | Enforcement | Audit |
|---|---|---|
| Neo4j | `$user_id` on every node/edge pattern | `tests/unit/test_cypher_safety.py` (auto-discovered modules) |
| PostgreSQL | `UserScopedRepository._select/_update/_delete` | `tests/unit/test_sql_safety.py` |
| Storage | `<root>/<user_id>/<uuid>` + charset check | `app/storage/local.py` |
| Agent | `user_id` is a Python parameter; tool arguments are `extra="forbid"` | `app/models/agent.py` |
| Provenance | `ProvenanceService` resolves through user-scoped repositories | integration tests |
| Evaluation data | not exposed over HTTP | — |

**New enforcement in Phase 9:**

1. **Route classification meta-test.** It enumerates `app.routes` and
   asserts every route is exactly one of:
   - *public* (allow-list: `/health`, `/health/ready`, `/auth/register`,
     `/auth/login`, `/docs`, `/openapi.json`, per Q6);
   - *internal* (`InternalTokenDep`);
   - *authenticated* (depends on `CurrentUser`).

   A new route that is none of these fails CI, the same way the Cypher
   meta-test works.
2. **Schema meta-test.** No request model (FastAPI body/query params
   collected from the OpenAPI schema) and no agent tool-argument model has a
   field named `user_id`, `owner`, `owner_id` or `userId`.
3. **Identity-injection tests** through authenticated HTTP (section 15).

**Status codes:**

| Condition | Code |
|---|---|
| No / invalid / expired / revoked credential | **401** |
| Valid credential, record owned by someone else or missing | **404** (unchanged) |
| Cookie-authenticated unsafe request with a bad Origin | **403** `origin_not_allowed` |
| Disabled account | **401** on existing sessions (they are revoked); **403** `account_disabled` at login after a correct password |

---

## 9. API Contract

All `/auth` endpoints are JSON and all errors use the existing error envelope
`{"error": {"code", "message", "request_id", "details?"}}`. There is no
`/auth/refresh`: the sliding idle expiry makes it unnecessary (section 6).

### `POST /auth/register`
- **Enabled only if** `AUTH_REGISTRATION_ENABLED=true` (Q2). Otherwise
  **404**, so the route's existence isn't revealed.
- Request: `{"email": str, "password": str}`, `extra="forbid"`. A
  `user_id` field is **rejected with 422**.
- **201** response: `{"user": {"id", "email", "status", "created_at"}}`.
  Registration does **not** log the user in. A separate login keeps one code
  path for session creation.
- Errors:

| Code | When |
|---|---|
| 409 `email_taken` | email already registered (accepted enumeration trade-off; section 4) |
| 422 | invalid email or password policy |
| 429 | rate limited |

- Writes the `users` row (UUID4) and the `user_accounts` row in **one
  transaction**.

### `POST /auth/login`
- Request: `{"email": str, "password": str, "token_in_body": bool = false}`.
- **200** response: `{"user": {...}, "session": {"expires_at", "idle_expires_at"}}`.
  - Always sets the `sb_session` cookie.
  - Adds `"token"` to the body only when `token_in_body=true`, which
    non-browser clients use. Browsers never need to see the token.
- Errors:

| Code | When |
|---|---|
| 401 `invalid_credentials` | unknown email *or* wrong password (identical body and similar timing) |
| 403 `account_disabled` | correct password, disabled account |
| 429 `rate_limited` | with `Retry-After` |

- On success, re-hashes the password if its parameters are outdated.

### `POST /auth/logout`
- Requires authentication.
- Revokes the **current** session (`revoked_at=now`) and clears the cookie
  (`Max-Age=0`).
- **204**. It is idempotent: an unauthenticated call returns 204 and clears
  the cookie, so a frontend can always call it.

### `POST /auth/logout-all`
- Requires authentication. Revokes **every** session of the user,
  including the current one. **204**.
- Justified as the remedy for a suspected stolen session.

### `POST /auth/password`
- Requires authentication. Request: `{"current_password", "new_password"}`.
- **204**. Revokes all *other* sessions.
- Errors: 401 `invalid_credentials` for a wrong current password (rate
  limited like login); 422 for the new-password policy.
- Justified because there are no reset emails: this is the only
  self-service password change.

### `GET /auth/me`
- Requires authentication.
- **200**: `{"user": {"id","email","status","created_at"}, "session": {"expires_at","idle_expires_at"}, "auth_mode": "session"}`.
- **401** when unauthenticated. This is the Phase 10 bootstrap call.
- In dev-header mode: `{"user": {"id": <header id>, "email": null, "status": "active", ...}, "auth_mode": "dev_header"}`, so dev UIs work.

### Existing endpoints

Every route classified *authenticated* in section 8 is covered. That is the
complete list from F2:

- memories, entities, search;
- graph overview, neighborhood, evidence and timeline;
- conversations and agent turns;
- documents and chunks, including provenance and sources;
- extraction jobs and review.

Behaviour changes:

- **Only:** a missing or invalid credential → 401.
- **Unchanged:** request and response shapes, 404-on-cross-user, and the
  Phase 8 budgets.
- `X-User-Id` in session mode: **ignored**. It is neither honoured nor
  rejected, so a stale client can't lock itself out of a valid session; this
  is pinned by a test. `/internal/*` is unchanged (operator token).

**401 response headers:**
- `WWW-Authenticate: Bearer realm="api"`;
- when the credential came from a cookie, `Set-Cookie` clearing it.

---

## 10. Graph API Security

The Phase 8 budgets are **unchanged and remain the hard ceiling**:

- nodes ≤ 1000, edges ≤ 3000, hops ≤ 3;
- memories per entity ≤ 25;
- evidence rows ≤ `SEARCH_MAX_LIMIT`, timeline ≤ 20;
- `/entities/{id}/graph` capped at 1000 nodes / 5000 edges.

Authentication is layered on top:

| Control | Rule |
|---|---|
| Authentication | Required on all four `/graph/*` routes and `/entities/{id}/graph`. There is no anonymous access in session mode. |
| Authorization | Unchanged: `$user_id` everywhere; foreign ids → 404; foreign `exclude_ids` → no effect. |
| Rate limit | Class `graph`, per user: **120 requests / 60 s** default (section 11). The measured worst case is ~1.2 s p50 at 10k entities, so 120/min bounds one user to about 2 cores of Neo4j time in the pathological case. |
| Max work per request | Unchanged budgets. Phase 9 adds no server-side Neo4j transaction timeout: none was needed at the Phase 8 budgets (Q9). |
| Max response size | Structurally bounded by the budgets. The measured maximum is ~230 KB for `/graph/*` and ~2.3 MB for the capped legacy endpoint. |
| Errors | 401 (auth) → 429 (rate) → 422 (validation) → 404 (ownership). This matches FastAPI order: dependencies before handler. |
| Local development | Dev-header mode keeps today's behaviour (anonymous → `DEV_USER_ID`). Impossible in production. |

---

## 11. Rate Limiting

**Mechanism: PostgreSQL fixed-window counters.** One table and one atomic
upsert per check:

```sql
INSERT INTO rate_limit_counters (bucket_key, window_start, count)
VALUES (:key, :window_start, 1)
ON CONFLICT (bucket_key, window_start) DO UPDATE SET count = rate_limit_counters.count + 1
RETURNING count;
```

- It runs in its **own** `db.session()` transaction, committed *before* the
  request continues (F12). A 401 or 429 therefore never rolls the count back.
- `bucket_key` = `"{class}:{identity}"`. The identity is `u:<user_id>`,
  `ip:<address>` or `email:<sha256(normalised email)>`; the raw email is
  never stored.
- Old windows are deleted in a bounded sweep: on a small random fraction of
  checks, `DELETE ... WHERE window_start < now - 2*window LIMIT 1000`-style
  via `ctid`, or on a startup hook. No scheduler is needed.

**Why not in-process?** An in-process counter resets on restart and is
per-worker, so with multiple uvicorn workers a brute-force limit multiplies.
That is acceptable for cheap classes but not for login.

**Why not Redis?** It is a new service, and the zero-cost/low-complexity
constraint rules it out. PostgreSQL is already on every request path after
Phase 9. The cost is one extra indexed upsert per limited request; it will
be **measured** during implementation, with a fallback noted in Q5.

| Class | Endpoints | Identity | Default limit | Burst note |
|---|---|---|---|---|
| `auth_login` | `POST /auth/login`, `POST /auth/password` | IP **and** email (both must pass) | 10 / 15 min per IP; 5 / 15 min per email | Counts every attempt, not only failures (simpler; no success-reset loophole) |
| `auth_register` | `POST /auth/register` | IP | 5 / hour | — |
| `graph` | `/graph/*`, `/entities/{id}/graph` | user | 120 / min | fixed window: up to 2× at a boundary (accepted) |
| `search` | `POST /search`, `/memories/search`, `/entities/search` | user | 120 / min | — |
| `agent` | `POST /conversations/{id}/agent/...` (LLM calls) | user | 30 / hour | protects paid-LLM spend |
| `message` | `POST /conversations/{id}/messages` (triggers extraction LLM calls) | user | 120 / hour | — |
| `ingest` | `POST /documents` | user | 20 / hour | existing size/page/chunk caps still apply |
| `write` | memory PATCH/invalidate, review POSTs | user | 120 / min | — |
| other reads | everything else authenticated | user | none in Phase 9 | budgets already bound per-request cost |

**Behaviour:**

- 429 with body code `rate_limited`, `Retry-After: <seconds to window end>`
  and `details.limit_class`.
- The limit check runs **after** authentication for user classes, so
  unauthenticated floods on user endpoints get cheap 401s and don't consume
  a user's quota. Auth classes run **before** credential verification.
- `RATE_LIMIT_ENABLED=true` by default. Tests set per-class limits
  explicitly. Each limit is configurable with bounds.

**IP limitation (explicit).** `request.client.host` is the direct TCP peer
(F18). Behind a reverse proxy it would be the proxy's address, collapsing
every client into one bucket. Phase 9 therefore adds `TRUSTED_PROXY_IPS`
(default empty):

- when empty, the peer address is used as-is;
- when set, and only when the direct peer is in that set, the right-most
  untrusted address of `X-Forwarded-For` is used.

`X-Forwarded-For` is **never** trusted from arbitrary peers, since that
would let any client pick its own rate-limit bucket. The per-email login
limit gives brute-force protection that doesn't depend on IP at all.

---

## 12. Development Identity Migration

**Settings:**

- `AUTH_MODE: "session" | "dev_header"`. Default `session`. `dev_header`
  reproduces today's exact behaviour.
- `ALLOW_USER_ID_HEADER` is **kept for compatibility** and is equivalent to
  `AUTH_MODE=dev_header`. When both are set and contradict each other, the
  process refuses to start. It is deprecated in the docs.

**Production hard-fail** (extends `_check_production_requirements`). With
`APP_ENV=production`, startup fails unless all of these hold:

- `AUTH_MODE=session`;
- `ALLOW_USER_ID_HEADER` is false or unset;
- `AUTH_COOKIE_SECURE=true`;
- explicit `CORS_ORIGINS` or `FRONTEND_URL` for credentialed requests;
- `CORS_ALLOW_VERCEL_PREVIEWS=false`, unless `CORS_VERCEL_PROJECT` narrows
  it (section 13);
- scrypt parameters at or above the OWASP floor.

Each failure gives a precise message, as today. **Any `APP_ENV` other than
`development` or `test`** is treated like production for the identity
checks. That closes the "typo'd or forgotten APP_ENV" hole (`staging`,
`prod`, empty).

**Compatibility:**

| Consumer | Change |
|---|---|
| Existing tests (942) | `tests/conftest.py` already sets `ALLOW_USER_ID_HEADER=true` → dev-header mode → **unchanged**. |
| New Phase 9 tests | Run with `AUTH_MODE=session` via a fixture: `auth_client(user)` registers and logs in through HTTP, or creates the account and session through the repository for speed, and returns a client with a Bearer header. |
| Eval harnesses (F13) | Service-level, so **unchanged**. `eval/graph_api_benchmark.py` explicitly sets dev-header mode in its settings override. |
| `scripts/seed_demo.py` | Unchanged: writes Neo4j for `demo-user`. |
| New `scripts/manage_users.py` | Operator CLI, no HTTP: `create --email [--user-id]` (attach an account to an existing legacy ownership id such as `demo-user`, creating the `users` row if missing), `disable`, `enable`, `reset-password`, `revoke-sessions`. Passwords come from an interactive prompt or stdin, **never argv**. |
| Local development | Recommended: `AUTH_MODE=session` with one CLI-created account. `dev_header` remains for quick API poking. `.env.example` documents both. |
| Agent, documents, retrieval, graph | No code change: they receive the same `user_id` string. |

---

## 13. Security & Secrets

| Item | Requirement |
|---|---|
| Signing secret | **None needed.** Opaque tokens are verified by lookup. The unused `AUTH_SECRET` in `.env.example` is removed (or documented as reserved) so nobody assumes it protects anything. Nothing in source code is secret. |
| Password hashing | Settings `AUTH_SCRYPT_N` / `_R` / `_P` / `_MAXMEM`, validated to be ≥ the OWASP floor in production. The parameters are stored per hash, so they can be upgraded. |
| Token storage | The DB stores only `sha256(token)`. Tokens, passwords, cookie values and `Authorization` headers are **never logged**. A logging filter masks `Authorization` / `Cookie` if a request is logged. Logs carry `user_id` and `session_id` (the row id, not the token). |
| Token expiry | Absolute 7 d, idle 24 h, both configurable within bounds (1 h – 30 d). |
| Refresh rotation | n/a: no refresh tokens. |
| Cookie | Name `sb_session`: `HttpOnly`, `Secure` (the dev default is `AUTH_COOKIE_SECURE=false` only when `APP_ENV=development`), `SameSite=Lax`, `Path=/`, `Max-Age` = absolute TTL. `AUTH_COOKIE_DOMAIN` unset by default (host-only). |
| CSRF | For **cookie-authenticated** `POST/PUT/PATCH/DELETE`: the `Origin` header must be present and in `allowed_origins` (or same-origin); otherwise 403 `origin_not_allowed`. `/auth/login` and `/auth/register` apply the same check when `Origin` is present (login-CSRF). Bearer-authenticated requests are exempt. `SameSite=Lax` is the second layer. |
| CORS (fix F7) | `allow_credentials=True` only with explicit origins. The `*.vercel.app` wildcard is removed; `CORS_ALLOW_VERCEL_PREVIEWS` becomes opt-in and requires `CORS_VERCEL_PROJECT=<slug>`, which turns the regex into `https://<slug>(-[a-z0-9-]+)?\.vercel\.app`. `allow_headers` is narrowed to `Authorization, Content-Type, X-Request-Id`. `allow_methods` stays `GET, POST, PATCH, OPTIONS` (plus `DELETE` only if a route needs it). The existing CORS tests are updated for the approved behaviour. |
| HTTPS | Assumed in production (terminated at the host/proxy). `Secure` cookies require it. Local HTTP is allowed only in development. |
| Env validation | The pydantic `Settings` validator covers all of the above; startup fails fast with a precise message. |
| Secret hygiene | `.env` stays ignored. Add a unit test that `.env.example` has empty values for every `*_SECRET` / `*_KEY` / `*_PASSWORD` / `*_TOKEN` key. |
| Error bodies | Auth errors never include the email, token, account id or the reason a session failed. They return one generic `authentication_required`, with the specific cause in server logs. |

---

## 14. Database Changes

One Alembic migration, **additive only**. Nothing in `users` or any
existing table changes. Neo4j: **no changes**.

```
user_accounts
  user_id            VARCHAR(128) PK, FK -> users.id ON DELETE CASCADE
  email_normalized   VARCHAR(254) NOT NULL UNIQUE
  password_hash      VARCHAR(255) NOT NULL            -- "scrypt$N$r$p$salt$hash"
  status             VARCHAR(16)  NOT NULL DEFAULT 'active'  CHECK (status IN ('active','disabled'))
  created_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
  updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
  password_changed_at TIMESTAMPTZ NOT NULL
  last_login_at      TIMESTAMPTZ  NULL

auth_sessions
  id                 UUID PK
  user_id            VARCHAR(128) NOT NULL FK -> users.id ON DELETE CASCADE, INDEX
  token_sha256       CHAR(64)     NOT NULL UNIQUE
  created_at         TIMESTAMPTZ  NOT NULL
  expires_at         TIMESTAMPTZ  NOT NULL
  last_used_at       TIMESTAMPTZ  NOT NULL
  revoked_at         TIMESTAMPTZ  NULL
  user_agent         VARCHAR(256) NULL   -- truncated, for the user's own session awareness; optional (Q10)

rate_limit_counters
  bucket_key         VARCHAR(200) NOT NULL
  window_start       TIMESTAMPTZ  NOT NULL
  count              INTEGER      NOT NULL
  PRIMARY KEY (bucket_key, window_start)
```

**SQL-safety audit.**
- `user_accounts` and `auth_sessions` rows are looked up *by credential*,
  not by a known user, so the auth repository is a documented audit
  exemption, like `user_repository.py` today.
  - Every lookup is by unique key (`email_normalized`, `token_sha256`).
  - Every write is keyed by the resolved `user_id`.
- `rate_limit_counters` holds no user data (hashed or opaque keys) and is
  exempt too.
- Each exemption is justified in the audit's `EXEMPT_FILES` comment.

There is no data migration. Legacy ownership ids gain accounts only through
the CLI.

---

## 15. Test Strategy

All tests are deterministic, with no LLM and no network. Every new
integration test cleans up in FK-safe order; the new tables are added to
`_PG_CLEANUP_STATEMENTS` before `users`.

**Unit (no DB)**
- scrypt hash/verify round trip, wrong password, parameter parsing,
  re-hash-needed detection, `maxmem` handling;
- email normalisation and password policy boundaries (11 / 12 / 128 / 129
  characters);
- token generation (length/entropy format) and SHA-256 storage;
- the settings matrix: every production hard-fail (section 12), non-dev
  `APP_ENV` treated as production, the Vercel regex narrowed to
  `CORS_VERCEL_PROJECT`, cookie `Secure` rules;
- credential extraction precedence (Bearer over cookie over dev header);
- the Origin-check function;
- `TRUSTED_PROXY_IPS` / `X-Forwarded-For` parsing, including spoofed chains;
- rate-limit window arithmetic and `Retry-After`;
- **meta-tests:** route classification (section 8.1), no ownership field in
  any request or tool-argument schema (8.2), `.env.example` secrets empty.

**Authentication (integration, PostgreSQL)**
- register 201; duplicate 409; `user_id` in the body → 422; registration
  disabled → 404;
- login 200 sets the cookie, and the token is absent from the body unless
  `token_in_body`;
- invalid password and unknown email → identical 401 bodies;
- logout revokes the session and a second use gives 401; logout-all revokes
  every session; logout while unauthenticated → 204;
- the expiry boundaries are exact: absolute expiry reached → 401; idle
  expiry reached → 401; a touch extends the idle window;
- malformed Bearer, unknown token, and a revoked token → 401;
- a disabled account: its existing session → 401, and login → 403 after a
  correct password;
- password change revokes other sessions and keeps the current one; a wrong
  current password → 401 and counts toward the limit;
- re-hash on login after the configured parameters are raised.

**Authorization (session mode, real HTTP, two real users)**
- A reads own memory → 200.
- A → B's memory, entity, entity graph, all four `/graph/*` endpoints,
  document, document chunks, provenance/source, conversation, agent turn in
  B's conversation, extraction job, review item → **404** each.
- Parametrised over every authenticated route from the classification
  meta-test, so a new route is covered automatically: "no credential → 401".

**Identity injection**
- `user_id` / `owner_id` in a JSON body → 422 (`extra="forbid"`) or ignored,
  per schema; asserted to never change ownership.
- `?user_id=B` query parameter → ignored; data stays A's.
- Forged `X-User-Id: B` with A's valid session → A's data. With no session
  → 401 (session mode).
- Path manipulation: B's ids in A's paths → 404.
- An agent turn whose scripted fake LLM emits tool arguments containing
  `user_id: B` → tool-argument validation error; no B data is touched.
  Extraction output naming another owner → ignored by schema.

**Graph**
- authenticated `/graph/*` → 200 with budgets unchanged (re-run the Phase 8
  conformance harness in session mode, 39/39);
- cross-user → 404;
- rate limit: request N+1 in a window → 429 with `Retry-After`; another
  user is unaffected.

**Documents**
- authenticated upload writes under `<root>/<A's user_id>/`;
- B cannot read A's document or chunks (404);
- the storage path uses the server-derived id.

**Agent**
- the turn runs as the session user;
- tools receive that id (spy);
- `propose_memory_update` writes are owned by the session user;
- no path lets the LLM select a user.

**Security**
- CORS: allowed origin + credentials OK; an arbitrary `*.vercel.app` gets no
  ACAO header; the narrowed project preview is OK;
- cookie attributes asserted from `Set-Cookie`;
- cookie-authenticated POST without `Origin` → 403, with a foreign `Origin`
  → 403, Bearer POST → OK;
- rate-limit counts **persist across a 401** (the F12 regression test);
- token and password never appear in captured logs.

**Regression**
- the full existing suite (942) in dev-header mode, unchanged;
- the Phase 8 conformance harness in both modes;
- `eval/retrieval_runner.py` byte-identical;
- the document eval 10/10;
- Cypher and SQL audits green;
- measure auth + rate-limit overhead per request (p50/p95 added latency) and
  report it.

---

## 16. Phase 9 Implementation Scope

**Will include:**
- the migration (section 14);
- `app/auth/` (or `app/services/auth_service.py` + `app/repositories/auth_repository.py`): hashing, sessions, account lifecycle;
- a rewritten `get_current_user_id` (section 6), with `CurrentUser` unchanged for routes;
- the `/auth` router: register, login, logout, logout-all, password, me;
- `app/services/rate_limit_service.py` + a dependency per class, wired to the section 11 endpoints;
- the Origin check (CSRF), the CORS hardening and the proxy-trust setting;
- settings and validators (section 12/13) and `.env.example` updates;
- `scripts/manage_users.py` (the operator CLI, including legacy-id attach);
- the tests (section 15), including the three meta-tests;
- docs:
  - `docs/API.md` (the auth section, 401/403/429 semantics, the rate-limit table);
  - ARCHITECTURE §15/§23;
  - `frontend/README.md` (replace the stale `X-User-Id` instruction);
  - README status;
  - ADR 0005 → superseded;
  - the new ADRs (section 19).

## 17. Explicit Non-Goals

- Frontend login UI and the Memory Explorer UI (Phase 10).
- OAuth / social login / OIDC. The `external_auth_id` seam is kept.
- Email verification, password-reset emails, magic links. There is no mail
  infrastructure.
- MFA, device management UI, session listing endpoint (Q10).
- Teams, organisations, sharing, RBAC, admin API or dashboard.
- Billing and usage plans.
- User deletion and data export/erasure (a separate data-lifecycle feature).
- Cloud deployment, TLS termination, WAF/DDoS protection.
- Redis, Docker, background schedulers.
- Server-side Neo4j query timeouts (Q9).
- Any change to memory, retrieval, graph, agent or extraction semantics.

---

## 18. Phase 10 Compatibility

**Required deployment property: same-site.** The browser must see the API
as same-site with the UI, either:

- (a) a Next.js rewrite/proxy (`/api/*` → backend) so the API is same-origin
  — zero cost, works on Vercel; or
- (b) API and UI on subdomains of one registrable domain.

`frontend/.env.example`'s separate `*.up.railway.app` host is **cross-site**.
Browsers increasingly block third-party cookies there, so cookie sessions
would fail in Safari and, in time, Chrome. This must be decided in Phase 10
planning (Q3). The Phase 9 cookie defaults (`SameSite=Lax`, host-only)
assume same-site.

**Flow:**
1. On load: `GET /auth/me` with credentials included.
   - 200 → the user is signed in;
   - 401 → show login.
2. Login: `POST /auth/login {email,password}`. The cookie is set; the UI
   never sees or stores the token. There is **no `localStorage` or
   `sessionStorage` token.**
3. API calls: `fetch(url, {credentials: "include"})`.
   - Unsafe methods send `Origin` automatically (browsers do).
   - No custom CSRF token is needed.
4. Any 401 → clear client state and route to login.
   - 403 `origin_not_allowed` means misconfiguration.
   - 429 → show `Retry-After`.
5. Logout: `POST /auth/logout` → 204.
   - "Sign out everywhere" → `POST /auth/logout-all`.
6. CORS: needed only if topology (b) is used. Then `CORS_ORIGINS` must list
   the exact UI origin. Topology (a) needs no CORS at all.
7. The rendering rule from Phase 8 stands: all content renders as text,
   never HTML. That rule is what keeps XSS from riding the cookie.

---

## 19. Proposed ADRs

| ADR | Title | Records |
|---|---|---|
| 0020 | Authentication uses server-side opaque sessions in PostgreSQL | B + D over JWT/OAuth; no refresh tokens (sliding idle expiry); token hashing; stdlib scrypt; supersedes ADR 0005 |
| 0021 | Authentication identity is separate from memory ownership identity | `users.id` stays the sole ownership key (string, legacy ids valid, UUID4 for new); credentials in `user_accounts`; the server always chooses `user_id`; no Neo4j change |
| 0022 | Browser credentials are HttpOnly SameSite=Lax cookies with an Origin check; deployment must be same-site | Cookie + Bearer dual transport; CSRF policy; the CORS hardening (F7); the Phase 10 topology requirement |
| 0023 | Rate limiting uses PostgreSQL fixed-window counters in their own transaction | Why not Redis or in-process; endpoint classes; the F12 transaction rule; the IP/proxy trust model |
| 0024 | Development identity is an explicit mode that cannot start in production | `AUTH_MODE`; the `ALLOW_USER_ID_HEADER` compatibility; the non-dev `APP_ENV` = production rule; test/eval strategy |

---

## 20. Zero-Cost / Dependency Audit

| Need | Provided by | New dependency? |
|---|---|---|
| Password hashing | `hashlib.scrypt` (stdlib, OpenSSL; verified working locally) | **No** |
| Token generation | `secrets.token_urlsafe` | No |
| Token hashing / constant-time compare | `hashlib.sha256`, `hmac.compare_digest` | No |
| Cookies, headers, CORS | FastAPI / Starlette (existing) | No |
| Storage for sessions, accounts, counters | existing PostgreSQL + SQLAlchemy + Alembic | No |
| Form/JSON parsing | pydantic (existing) | No |

- **Optional alternative requiring approval (Q1):** `argon2-cffi` for
  Argon2id, OWASP's first preference. It is zero-cost (MIT, wheels
  available) but is one new native dependency. It is **not** required, since
  scrypt is an OWASP-listed alternative.
- No Redis, no Docker, no paid or cloud auth provider, no JWT library.
- Local Neo4j Community and local PostgreSQL remain the only stores.
- The LLM and embedding abstractions are untouched.
- `cryptography` exists only transitively and is **not** to be imported
  directly.

---

## 21. Risks & Open Questions

**Risks**

| Risk | Mitigation |
|---|---|
| Counters written in the request transaction get rolled back (F12), silently disabling brute-force protection | Their own transaction, plus a dedicated regression test |
| A PG lookup on every request adds latency; PG becomes a hard dependency for Neo4j-only routes (PG down → 503 everywhere) | Measure p50/p95 overhead; `/health/ready` already reports PG; session-touch throttling |
| Cookie auth fails in a cross-site deployment | A same-site requirement recorded in ADR 0022 and decided in Phase 10 planning (Q3) |
| Scrypt parameters too heavy for a small host (128 MiB per concurrent login) | Configurable, measured; login rate limits bound concurrency; documented OWASP alternatives |
| A test suite that stays in dev-header mode could hide auth regressions | The session-mode suite, including the route meta-test, runs on every CI run |
| The CORS change breaks an existing Vercel preview workflow | Opt-in `CORS_VERCEL_PROJECT`; documented |
| Registration enumeration (409) | Rate-limited; accepted and documented (Q7) |

**Open questions requiring approval**

1. **Q1 — Hashing:** stdlib scrypt (recommended, zero new dependencies) or
   Argon2id via `argon2-cffi`?
2. **Q2 — Registration:** open (`AUTH_REGISTRATION_ENABLED=true`) or
   CLI-only by default in production? Recommended: enabled in development,
   **disabled by default in production** (single-owner deployments create
   accounts with the CLI).
3. **Q3 — Phase 10 topology:** Next.js same-origin proxy (recommended, no
   CORS) or a shared parent domain? Needed before Phase 10, and it confirms
   the cookie defaults.
4. **Q4 — Session lifetimes:** 7 d absolute / 24 h idle acceptable?
5. **Q5 — Rate-limit defaults** (section 11 table), and whether per-request
   PG counter overhead is acceptable if measured above ~5 ms p95. Fallback:
   in-process counters for the `graph`/`search`/`write` classes only, with
   auth classes always in PostgreSQL.
6. **Q6 — `/docs` and `/openapi.json` in production:** keep them public
   (recommended; they expose schema, not data) or disable them?
7. **Q7 — Accept the registration enumeration trade-off** until email
   delivery exists?
8. **Q8 — Legacy ownership ids:** attach accounts to `dev-user` /
   `demo-user` through the CLI (recommended), or leave legacy data
   unreachable in session mode?
9. **Q9 — Neo4j transaction timeout:** keep deferring it (the Phase 8
   budgets measured at ≤ ~1.9 s), or add a conservative server-side
   `timeout` on graph reads now?
10. **Q10 — Session listing:** add `GET /auth/sessions` and per-session
    revoke (needs `user_agent`), or keep only logout-all?

---

## 22. Recommended Implementation Order

1. **Characterise first.** Add the route-classification and
   no-ownership-field meta-tests against the current code (they should pass
   in dev mode). Add a characterisation test of today's lazy provisioning
   and CORS behaviour.
2. **Settings and validators** (`AUTH_MODE`, cookie, CORS, proxy trust,
   scrypt, rate limits) with unit tests. Keep the defaults compatible with
   dev mode so the existing suite stays green.
3. **Migration + repositories** (accounts, sessions, counters) + the
   SQL-audit exemptions.
4. **Password hashing + account service** + unit tests; measure the scrypt
   parameters on the dev machine.
5. **Session service + the new `get_current_user_id`** (Bearer/cookie/dev);
   the `/auth` router; the CLI.
6. **The Origin check + CORS hardening**; update the CORS tests to the
   approved behaviour.
7. **Rate-limit service** in its own transaction; wire the classes; the F12
   regression test.
8. **Authorization and identity-injection suites** in session mode; re-run
   the Phase 8 conformance harness in session mode.
9. **Measure overhead** (auth + rate limit per request), plus the full
   regression and evals.
10. **Docs + ADRs 0020–0024**; mark ADR 0005 superseded; update
    `frontend/README.md`.
11. **One checkpoint commit** (on approval).

## 23. Acceptance Criteria

1. In session mode, every route classified *authenticated* returns 401
   without a valid credential. This is proven by the auto-discovering route
   meta-test. `X-User-Id` has no effect.
2. No request body, query, path or tool argument can set or change the
   owning `user_id`, proven by schema meta-tests and injection tests.
3. Every cross-user access through authenticated HTTP returns 404, for every
   resource type in section 15.
4. Registration, login, logout, logout-all, password change and `me` behave
   exactly per section 9, including the expiry boundaries and disabled
   accounts.
5. Tokens and passwords are never stored in plaintext or logged.
6. Production startup fails for each unsafe configuration in section 12.
7. Credentialed CORS admits only explicit origins; arbitrary `*.vercel.app`
   origins are refused.
8. Cookie-authenticated unsafe requests without an allowed Origin → 403.
9. Rate limits return 429 with `Retry-After` per class. Counts persist
   across 401 responses. Limits are per user (or IP/email for auth). The
   IP-proxy behaviour matches section 11.
10. Phase 8 graph budgets are unchanged; the conformance harness passes
    39/39 in both modes.
11. The full pre-existing suite passes unchanged in dev-header mode. Retrieval
    eval output is byte-identical; document eval 10/10; `ruff check app
    tests eval` is clean.
12. Measured per-request auth + rate-limit overhead is reported with real
    numbers.
13. No new dependency (unless Q1 approves `argon2-cffi`), no Redis, no
    Docker, no paid service; Neo4j schema untouched.
14. Docs and ADRs are updated per section 16; ADR 0005 is marked superseded.

---

## 24. Implementation Status (Phase 9 complete)

Approved decisions:

| Q | Decision |
|---|---|
| Q1 | stdlib scrypt |
| Q2 | registration off in production (CLI accounts) |
| Q3 | Next.js same-origin proxy |
| Q4 | 7 d absolute / 24 h sliding idle |
| Q5 | section 11 defaults; counters survive failures |
| Q6 | `/docs` public |
| Q7 | enumeration accepted; no email |
| Q8 | CLI attach for `dev-user` / `demo-user` |
| Q9 | no Neo4j timeout |
| Q10 | no session listing |

Measurement environment: one local machine, local PostgreSQL + Neo4j
Community, synthetic data.

### 24.1 What was built (as planned unless listed in 24.2)

| Area | Implementation |
|---|---|
| Schema | Migration `320f6d898a92`, additive only: `user_accounts`, `auth_sessions`, `rate_limit_counters`. `users` and all existing tables and Neo4j are untouched. |
| Primitives | `app/auth/` — passwords (scrypt, policy, email normalisation), tokens (`sbs_` + 256-bit, SHA-256 digest), HTTP helpers (credential extraction, Origin check, trusted-proxy client IP) |
| Services | `app/services/auth_service.py` (accounts, sessions, revocation, operator operations) and `app/services/rate_limit_service.py` (fixed windows, own transaction per hit) |
| Identity dependency | `app/api/deps.py::get_auth_context` → `get_current_user_id` → `CurrentUser` (unchanged for every route) |
| API | `app/api/routes/auth.py`: register / login / logout / logout-all / password / me |
| Rate limits | Wired as `RATE_LIMIT_*` dependencies on the section 11 routes (graph router-level) |
| CORS | Explicit origins; one named Vercel project only; narrowed headers |
| Startup guards | Section 12 guards in `Settings._check_production_requirements`; empty optional values mean "unset"; malformed `TRUSTED_PROXY_IPS` refuses to start |
| Operator CLI | `scripts/manage_users.py` |

### 24.2 Deviations and clarifications (none architectural)

1. **scrypt default N=2^15, r=8, p=3** instead of the plan's first-listed
   N=2^17. The Stage 0 micro-measurement on this machine gave N=2^17 ≈ 417 ms
   (above the plan's 100–300 ms target) and N=2^15/p=3 ≈ 293 ms. Both are
   OWASP-listed, and the production floor accepts either.
2. **`POST /memories` is in the `write` class.** The section 11 table
   listed only PATCH / invalidate / review; memory creation is the same class
   of write.
3. **No `user_agent` column.** It existed only to support session listing,
   which Q10 declined.
4. **The production CORS/origin requirement is kept under Q3.** The Next.js
   proxy forwards the browser's `Origin`, and the CSRF check needs the
   frontend origin on the allow-list.
5. **Added (security review): scrypt runs off the event loop**
   (`asyncio.to_thread`). The first benchmark run showed that inline hashing
   blocks every other request during a login (section 24.4). A
   mutation-verified test pins it.
6. **Autogenerate artefact removed.** `alembic --autogenerate` proposed
   dropping the existing `memory_embeddings_hnsw_cosine` index (not declared
   on the ORM model). The line was removed; the migration stays additive, and
   the index was verified present after upgrade.
7. **`/auth/logout-all` and `/auth/password` need a real session.** In
   dev-header mode there is none, so they return `401`.

### 24.3 Verification

| Check | Result |
|---|---|
| Full suite (final) | **1258 passed, 0 failed, 0 skipped** (942 pre-existing + 316 new) |
| `ruff check app tests eval scripts/manage_users.py` | clean |
| Retrieval eval (`eval/retrieval_runner.py`) | **byte-identical** to `e028188` (72 lines, pristine worktree vs working tree) |
| Graph API conformance (`eval/graph_api_runner.py`) | **39/39**, persisted run `d9690cfd…` |
| Document eval (deterministic) | 10/10, 0 wrong merges |
| Graph API benchmark (100 scale, dev-header pinned) | 0 budget violations; canonical-question request counts unchanged (q2: 21 → 1) |
| `eval/runner.py` (Phase 2 extraction) | **fails — pre-existing, reproduced on `e028188`.** Its cleanup deletes `extraction_candidates` before the FK-dependent `review_audit_log` / `memory_review_candidates`, the same bug class Phase 7 found in other harnesses. Not fixed here (out of scope); the synthetic rows its failed runs left in the test DB were removed. |
| Structural audits | Mutation-checked: an injected unauthenticated route and an injected `owner_id` request field were both caught |

### 24.4 Measured authentication overhead (`eval/auth_benchmark.py`)

**Run 2** is authoritative: load average ≈ 5–7, after the event-loop fix.
Timings are p50 (p95); the dev-header column is the pre-Phase-9 path with no
identity DB work.

**Login** (full `POST /auth/login`):

| scrypt cost | Success | Wrong password | Unknown email |
|---|---|---|---|
| Default N=2^15 r=8 p=3 | 193 ms (225) | 192 ms (213) | 190 ms (375) |
| N=2^17 r=8 p=1 | 262 ms (285) | 260 ms (273) | 262 ms (514) |

**Authenticated requests** (60 trials each):

| Request | dev-header | session | session + rate limit |
|---|---|---|---|
| `GET /auth/me` | 1.86 ms | 3.16 ms (+1.30) | 3.32 ms (+1.46) |
| `GET /memories/search` | 13.67 ms | 14.26 ms (+0.59) | 17.84 ms (+4.17) |
| `GET /graph/overview` | 26.44 ms | 26.75 ms (+0.32) | 28.26 ms (+1.82) |
| `GET /graph/entities/{id}/neighborhood` | 16.06 ms | 17.73 ms (+1.67) | 19.54 ms (+3.48) |

One rate-limit counter operation measured **1.46 ms p50 / 2.03 ms p95**. That
is under the Q5 ~5 ms p95 threshold, so the in-process fallback was **not**
needed.

**Run 1**, for honesty: load average ≈ 30, *before* the event-loop fix.
- Default-cost login measured 1329 ms p50 / 3740 ms p95.
- Session + rate-limit p95 reached 235–341 ms.

This run is what exposed the blocking hash. It is kept as evidence, not as a
result.

### 24.5 Security review (Stage 17)

**How to read this.** "Verified" means covered by tests. "Inspected" means
verified by reading the code. "Assumed" means a design assumption.

| Item | Finding | Basis |
|---|---|---|
| Plaintext passwords | Only scrypt hashes are stored; the hash never contains the password | verified (unit + DB assertion) |
| Plaintext tokens | Only SHA-256 digests are stored | verified (DB assertion) |
| Token/password logging | Neither appears in any log record across login, request, failed login and logout; events are logged by user/session id | verified (log-capture test) |
| Randomness | `secrets.token_urlsafe(32)`; `os.urandom` salts | inspected |
| Password comparison | `hmac.compare_digest`; unknown email costs a dummy scrypt | inspected + measured (≈ equal p50s) |
| Session fixation | Every login creates a new session; a presented cookie is never adopted | verified |
| Logout / logout-all / password change / disable revocation | Immediate | verified |
| `user_id` trust from clients | No route input, tool argument or LLM-output schema carries ownership; body `user_id` → 422; query `user_id` ignored; forged `X-User-Id` ignored with a session and → 401 without one | verified (structural + HTTP) |
| Agent identity switching | An adversarial scripted LLM (tool argument `user_id`, the other user's memory ids, a citation of them) reached none of the other user's content, and the citation was not accepted | verified |
| Cross-user access | 404 for memory, provenance, patch, invalidate, entity, relationships, entity graph, all `/graph/*`, conversation, messages, document and chunks; the overview and search are empty | verified |
| CORS | The arbitrary `*.vercel.app` wildcard is gone; only the named project matches; header allow-list narrowed | verified |
| Origin check (CSRF) | Cookie POST without `Origin` / with a foreign `Origin` → 403; allowed and same-origin → OK; Bearer exempt; login and logout checked | verified |
| Forwarded IP | Ignored from untrusted peers; right-most untrusted hop from trusted proxies; garbage rejected | verified |
| Dev bypass in production | Refused for every non-dev `APP_ENV` value tested (`production`, `staging`, `prod`, empty, `Production`) | verified |
| Registration abuse | Off by default in production; IP-limited | verified |
| Error leakage | One generic 401 body; wrong password ≡ unknown email; `403 account_disabled` only after a correct password | verified |
| Cookie configuration | `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age`; `Secure` forced outside dev/test | verified (dev attributes + prod guard) |
| Event-loop DoS via login | Found and fixed (item 5 in 24.2) | measured + verified |
| Secrets in git | `.env` untracked; `.env.example` has no secret values (test); unused `AUTH_SECRET` removed | verified |

### 24.6 Known limitations (not claimed as solved)

* **Account lockout.** The per-email login limit lets anyone who knows an
  email block its logins for one 15-minute window. This is the inherent
  trade-off of per-account throttling.
* **Fixed windows** admit up to 2× a limit across a boundary.
* **Rate limiting is not startup-enforced.** `RATE_LIMIT_ENABLED=false` is
  honoured in every environment.
* **Behind a proxy**, per-IP limits require `TRUSTED_PROXY_IPS`. Otherwise
  every client shares the proxy's bucket (the per-email limit is unaffected).
* **Registration enumerates emails** (409), accepted per Q7.
* **Token lookup timing.** Lookup goes through a B-tree on the SHA-256
  digest, not in constant time. This is deferred and assessed as low risk
  (section 4).
* **Dummy-hash timing.** The first unknown-email login after a restart
  computes the cached dummy hash, so it does roughly double work once.
* **The idle window uses touch granularity** (`AUTH_SESSION_TOUCH_SECONDS`,
  default 300). The effective idle timeout can therefore be up to 5 minutes
  shorter than 24 h.
* **Review endpoints are not in the cross-user HTTP matrix.** Their isolation
  remains covered by the existing Phase 4 service tests, not by a new
  session-mode HTTP test.
* **The development DB is not migrated.** Only the test database was
  migrated during implementation; run `uv run alembic upgrade head` against
  it before using session mode locally.
* **XSS within an open page** is not prevented by `HttpOnly`. The Phase 8
  render-as-text contract remains required for Phase 10.
