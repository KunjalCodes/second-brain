# API.md

# AI Second Brain — Phase 1 + Phase 2 Reference

Data model, graph schema, endpoints, and how to run everything locally.

**Scope.** Phase 1 is the deterministic graph-memory core: memories can be created,
stored in Neo4j, retrieved, updated, invalidated and traversed — no LLM. Phase 2 adds
conversations and messages (PostgreSQL), and LLM-proposed memory extraction governed
by a deterministic decision engine (docs/adr/0007): the model proposes candidates,
application code decides what becomes memory, Neo4j remains the only canonical store.
There is still **no embeddings, no vector search, no agent, no document ingestion and
no authentication** — those are later phases.

Sections 1–4 below are Phase 1 (unchanged). New Phase 2 material is in sections
1a–4a. Running/testing instructions (sections 5–6) cover both phases together.

---

# 1. Data model

## Memory

The canonical unit: one atomic statement, with provenance and a validity window.
See [ADR 0001](adr/0001-memory-as-canonical-unit.md).

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `user_id` | string | Namespace. Present on every record. |
| `content` | string | 1–10,000 chars. **Immutable** — see §4. |
| `memory_type` | enum | `episodic` \| `semantic` \| `procedural` \| `working` |
| `status` | enum | see below |
| `confidence` | float | 0.0–1.0. Metadata, not ground truth (`CLAUDE.md` §17). |
| `valid_from` | datetime | Inclusive. Defaults to creation time. |
| `valid_until` | datetime \| null | **Exclusive.** `null` = still true. |
| `created_at` | datetime | Transaction time. |
| `updated_at` | datetime | Transaction time. |
| `invalidation_reason` | string \| null | Set when retracted. |

### Status

| Status | Meaning |
|---|---|
| `active` | Currently believed true. |
| `historical` | Was true, window closed, nothing replaced it. |
| `superseded` | Explicitly replaced by a newer memory. |
| `invalid` | Retracted; treated as never having been true. |
| `conflicting` | Contradicts another memory; unresolved. |
| `archived` | Retained for the record, excluded from all retrieval. |

Legal transitions are an explicit table in `app/models/lifecycle.py`. A closed
memory (`historical`, `superseded`, `invalid`) can never return to `active` — 
re-asserting a fact creates a new memory. `archived` is terminal.

`invalid` and `archived` are hidden from retrieval by default; `historical` and
`superseded` remain visible, because inspecting superseded memory is a product
requirement (`PRODUCT.md` §7).

## Entity

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `user_id` | string | |
| `name` | string | As first written. |
| `normalized_name` | string | Identity key: lowercased, whitespace collapsed. |
| `entity_type` | enum | `person` \| `project` \| `company` \| `technology` \| `concept` \| `document` \| `decision` \| `event` \| `task` \| `other` |
| `aliases` | string[] | Unioned across mentions, never replaced. |

**Phase 1 entity resolution is normalization plus explicit aliases only.** `EasyOCR`,
`easyocr` and `  EasyOCR  ` resolve to one entity. `Open AI` and `OpenAI` do **not** —
stripping internal spaces would also merge `Data Base` with `Database`, and wrongly
merging two entities destroys memory, which is worse than failing to merge. That case
needs an explicit alias until Phase 4 adds semantic resolution.

`entity_type` is set by the first assertion and is not overwritten by later mentions.

## Relationship (`RELATES_TO`)

A temporally-scoped edge, traceable to the memory that asserted it.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `user_id` | string | |
| `predicate` | string | e.g. `uses`. Shape `^[a-z][a-z0-9_]{0,62}[a-z0-9]$`. |
| `source_entity_id` / `target_entity_id` | uuid | |
| `status` | enum | Same vocabulary as Memory. |
| `confidence` | float | |
| `memory_id` | uuid | **Provenance**: which memory asserted this edge. |
| `valid_from` / `valid_until` | datetime | Same half-open semantics. |

One predicate carries the validity window; `currently_uses` / `previously_used` are
*derived views*, not stored predicates. See
[ADR 0003](adr/0003-relationship-representation.md).

## Source

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `source_type` | enum | `conversation` \| `message` \| `document` \| `url` \| `tool_result` \| `user_statement` \| `system_inference` |
| `reference` | string \| null | e.g. `conversation-101` |
| `location` | string \| null | e.g. page or offset |

Sources are deduplicated per user on `(source_type, reference, location)`, so many
memories from one conversation share a single Source node.

## Phase 2 additions to the memory model

**`sources` is many-to-many.** A `DUPLICATE`-classified candidate attaches an
additional Source to an existing memory rather than creating a new one, so a
corroborated memory can cite more than one source. `GET /memories/{id}` now returns
`sources: [...]` (a list) instead of a singular `source`.

**`conflicts_with`.** A `CONFLICTING`-classified candidate creates a new memory with
`status: conflicting` and links it to the memory it contradicts via a
`(:Memory)-[:CONFLICTS_WITH]-(:Memory)` edge. Neither memory is modified — both
remain fully visible. `GET /memories/{id}` returns `conflicts_with: [<memory ids>]`.

---

# 1a. Conversations and extraction (Phase 2)

**PostgreSQL holds application and conversation state. It never duplicates the
memory graph** — see [ADR 0006](adr/0006-postgresql-for-application-state.md). Data
flows one way: `PostgreSQL (conversations, messages) → extraction pipeline → Neo4j`.

```text
users            identity reference only -- no passwords, no credentials (Phase 8)
conversations    user_id, title, timestamps
messages         conversation_id, role, content, sequence (monotonic per
                 conversation), extraction_status
extraction_jobs  one unit of work over a window of messages; claim/lease/retry state
extraction_candidates
                 what the LLM proposed and what the engine decided -- the audit
                 trail between "LLM proposed" and "engine decided"
                 (docs/PHASE2_PLAN.md section 4). NOT canonical memory: `memory_id`
                 is an opaque pointer into Neo4j, never a copy of memory state.
entity_merge_candidates
                 flagged possible entity duplicates (e.g. "OpenAI" / "Open AI") --
                 recorded for review, never auto-merged (docs/adr/0007)
```

## Extraction pipeline (docs/adr/0008)

```text
append message  →  deterministic gate (no LLM)  →  create/extend extraction job
                       │
                       ▼ (after the HTTP response is returned)
                background task: claim job → LLMClient.complete_structured
                       │
                       ▼
                Pydantic validation → grounding check → entity resolution
                       │
                       ▼
                decision engine: NEW / DUPLICATE / UPDATE / SUPERSEDING /
                                 CONFLICTING / INVALID / IGNORE
                       │
                       ▼
                persist via the unchanged Phase 1 MemoryService → Neo4j
```

The gate and job creation are synchronous (no LLM call, so no added latency); the
model call itself is a `BackgroundTasks` job that FastAPI runs only after the
response is sent. A periodic **sweep** (`POST /internal/extraction/sweep`, or
`scripts/run_sweep.py`) reclaims jobs whose worker died and re-evaluates messages
the live-path gate skipped, bounded by `EXTRACTION_SWEEP_BATCH_SIZE` and
`EXTRACTION_SWEEP_LOOKBACK_DAYS`.

**The LLM never controls the database** ([ADR 0007](adr/0007-llm-proposes-application-decides.md)):
its output names entities and relationships by string, never by id, and contains no
field meaning "delete" or "merge". Every candidate's `source_span` is verified
against the real message text before anything is persisted — a fabricated fact with
no textual anchor is rejected as `INVALID`, not argued with.

## The seven decisions

| Decision | When | Effect |
|---|---|---|
| `NEW` | No existing fact for this (source, predicate) | Creates a memory |
| `DUPLICATE` | Exact restatement of an active fact | Attaches a source to the existing memory |
| `UPDATE` | Same fact restated with higher confidence, or a change signal naming the same target | Bumps confidence on the existing memory |
| `SUPERSEDING` | A change signal names a different, existing target | Closes the old memory/edge, creates a new one (Phase 1's supersession path, unchanged) |
| `CONFLICTING` | A different active fact exists, no change signal | Creates a new memory, links `CONFLICTS_WITH` — **never overwrites** |
| `INVALID` | Failed grounding or domain validation | Nothing persisted |
| `IGNORE` | `persistence != "persistent"` | Nothing persisted |

**Known limitation, narrowed but not eliminated in Phase 3:** duplicate/conflict
detection compares the *canonicalized triple* `(source, predicate, target)`
first, unchanged. "uses Gemini for classification" and "classifies with Gemini"
now get a second chance via semantic evidence (section 1b) when the exact check
finds nothing to compare against — but that evidence only ever produces
`LIKELY_DUPLICATE`/`FLAGGED`, both persisted exactly like `NEW`. It never merges
or overwrites; a human (or a later phase) reviews the flag.

---

# 1b. Embeddings and semantic evidence (Phase 3, ADR 0009)

`memory_embeddings` (PostgreSQL + pgvector) holds a disposable, regenerable
vector for each memory's content, keyed by `(memory_id, embedding_provider,
embedding_model)` — never overwritten in place, so a model migration can run
side-by-side with the old one. `memory_id` is an opaque pointer into Neo4j, the
same pattern as `extraction_candidates.memory_id` — Neo4j gains no new node,
property, or relationship type for this.

```text
Neo4j Memory.id  (created first, always)
        │
        ▼
PostgreSQL memory_embeddings.memory_id  (created after, asynchronously,
                                          at-least-once -- a missing/lagging
                                          row is never treated as "memory does
                                          not exist")
```

New `extraction_candidates.decision` values: `LIKELY_DUPLICATE`, `FLAGGED` —
both are semantic-evidence outcomes (`app/semantic/similarity.py`), gated on
high embedding similarity *and* shared entity context, never similarity alone.
`entity_merge_candidates` (unchanged table from Phase 2, `score` column now
populated) gains the same signal for entity names: an embedding-similarity match
is recorded for review, never auto-merged, alongside the existing exact
canonical-key check.

---

# 2. Graph schema

```
                    ┌──────────────┐
                    │    Source    │
                    └──────▲───────┘
                           │ DERIVED_FROM
                           │
   SUPERSEDES       ┌──────┴───────┐
  ┌───────────────► │    Memory    │
  │                 └──────┬───────┘
  │                        │ MENTIONS
┌─┴────────┐               ▼
│  Memory  │        ┌──────────────┐    RELATES_TO     ┌──────────────┐
└──────────┘        │    Entity    ├──────────────────►│    Entity    │
                    └──────────────┘  {predicate,      └──────────────┘
                                       valid_from,
                                       valid_until,
                                       status,
                                       memory_id}
```

**Nodes:** `Memory`, `Entity`, `Source`
**Relationships:** `MENTIONS`, `DERIVED_FROM`, `SUPERSEDES`, `RELATES_TO`

## Constraints and indexes

Applied by `scripts/init_graph.py`. **Community Edition features only**, so the same
schema works locally and on Aura.

```
CONSTRAINT memory_id_unique         (:Memory)    id                          UNIQUE
CONSTRAINT entity_id_unique         (:Entity)    id                          UNIQUE
CONSTRAINT source_id_unique         (:Source)    id                          UNIQUE
CONSTRAINT entity_identity_unique   (:Entity)    (user_id, normalized_name)  UNIQUE

INDEX memory_user_status       (:Memory)      (user_id, status)
INDEX memory_user_created      (:Memory)      (user_id, created_at)
INDEX memory_user_type         (:Memory)      (user_id, memory_type)
INDEX entity_user_type         (:Entity)      (user_id, entity_type)
INDEX source_user              (:Source)      (user_id)
INDEX relates_to_user_status   [:RELATES_TO]  (user_id, status)
INDEX relates_to_predicate     [:RELATES_TO]  (predicate)
INDEX relates_to_memory_id     [:RELATES_TO]  (memory_id)
```

`relates_to_memory_id` (Phase 4): every memory read (`GET_MEMORY`) does an
`OPTIONAL MATCH` on `RELATES_TO` filtered by `memory_id` to find that memory's
own asserted edges. The dense-graph benchmark (`eval/graph_scale_benchmark.py`,
PHASE4_PLAN.md section 14) measured this as an unindexed full scan of every
`RELATES_TO` edge for the user before this index existed — ~425ms per memory
read at 219,782 edges, versus ~2ms on a sparse graph — which was the actual
cause of hybrid retrieval's dense-graph latency (39-113s at 10,000 synthetic
nodes), not the multi-hop traversal itself. Adding this index brought it back
to ~2ms regardless of graph density (hybrid search at the same scale: ~580ms).
No fan-out cap was added — the benchmark showed a missing index, not too many
candidates, was the cause.

`entity_identity_unique` is load-bearing: without it, two concurrent requests
mentioning the same entity could `MERGE` duplicate nodes. It is composite on
`user_id`, so two different users may each have their own "Drawing Extractor".

Node key and property existence constraints are Enterprise-only and therefore **not**
used; required-field validation lives in the service layer instead.

---

# 3. User isolation

Every node and relationship carries `user_id`, and **every Cypher statement filters
on `$user_id`**. This is enforced three ways:

1. All Cypher lives in module-level constants in `app/repositories/`, and
   `tests/unit/test_cypher_safety.py` statically asserts every one references
   `$user_id` — a new query that forgets the filter fails CI.
2. Multi-hop traversal requires *every node and relationship along the path* to
   belong to the requesting user, so a traversal cannot leave the user's subgraph.
3. Cross-user access returns **404, not 403**, so the API never confirms that another
   user's record exists.

**Identity (Phase 9).** The `user_id` every query filters on is derived ONLY
from a verified server-side session (section 3a). The pre-Phase-9 `X-User-Id`
header survives solely as the explicit development mode `AUTH_MODE=dev_header`,
which cannot start in any production-like environment
([ADR 0024](adr/0024-development-identity-cannot-run-in-production.md);
[ADR 0005](adr/0005-phase1-identity-placeholder.md) is superseded).

---

# 3a. Authentication (Phase 9)

Local email + password accounts; **server-side opaque sessions** stored in
PostgreSQL (only the token's SHA-256), password hashes by the standard
library's scrypt. No JWT, no refresh tokens, no OAuth, no email flows
([ADR 0020](adr/0020-postgresql-server-side-sessions.md)).

## Identity model

| | |
|---|---|
| Memory ownership id | `users.id` — unchanged; the ONE key every PostgreSQL row, Neo4j node/edge and storage directory is owned by. |
| Login identity | `user_accounts.email_normalized` (trimmed, lower-cased, unique) — separate from ownership ([ADR 0021](adr/0021-auth-identity-separate-from-memory-ownership.md)). |
| Who chooses `user_id` | Always the server: UUID4 at registration, or an existing legacy id attached by the operator CLI. No request body, query, path, header, tool argument or LLM output can set it (enforced by structural tests). |

## Credentials

| Client | Transport |
|---|---|
| Browser | `sb_session` cookie: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=604800`, `Secure` outside development/test, host-only. JavaScript never sees the token. |
| Scripts, tests, eval | `Authorization: Bearer <token>`; request the token with `"token_in_body": true` at login. |

`Authorization` wins over the cookie. Any presented credential is always
verified (a malformed or unknown one is `401`, never a silent fallback), and
when one is present `X-User-Id` is ignored.

**Sessions** last at most **7 days** (absolute) and expire after **24 hours
idle**; the idle window slides on use (`last_used_at` is written at most every
`AUTH_SESSION_TOUCH_SECONDS`, default 300). Boundaries are exact: at
`expires_at`, or `last_used_at + 24h`, the session is refused. Every login
creates a new session (no fixation); several concurrent sessions per account
are allowed.

## Endpoints

| Method | Path | Auth | Success | Notes |
|---|---|---|---|---|
| `POST` | `/auth/register` | none | `201 {"user"}` | `{"email","password"}` (`extra` fields incl. `user_id` → 422). Password 12–128 chars, not the email. `409 email_taken` on duplicate (accepted enumeration trade-off, rate-limited). **Disabled by default in any production-like environment** → identical to an unknown route (`404 {"detail":"Not Found"}`). Does not log in. |
| `POST` | `/auth/login` | none | `200 {"user","session":{"expires_at","idle_expires_at"},"token"?}` + cookie | `{"email","password","token_in_body"?}`. Wrong password and unknown email are the same `401 invalid_credentials` with the same scrypt work. `403 account_disabled` only after a correct password. Re-hashes on login if the configured scrypt cost changed. |
| `POST` | `/auth/logout` | optional | `204` | Revokes the presented session; always clears the cookie; idempotent. |
| `POST` | `/auth/logout-all` | session | `204` | Revokes **every** session of the account. |
| `POST` | `/auth/password` | session | `204` | `{"current_password","new_password"}`; wrong current → `401` (counts toward the login limits); revokes every **other** session, keeps this one. |
| `GET` | `/auth/me` | any | `200 {"user":{"id","email","status","created_at"},"session","auth_mode"}` | The Phase 10 bootstrap call. Never returns hash or token material. In dev-header mode: `email: null`, `session: null`, `auth_mode: "dev_header"`. |

There is deliberately **no** refresh endpoint (the idle window slides), no
session listing (only logout-all), no password-reset email and no email
verification. Password reset and account disable/enable are operator-only
(section 3a "Operator CLI").

## Errors

| Status | `error.code` | When |
|---|---|---|
| `401` | `authentication_required` | Missing, malformed, unknown, expired, idle, revoked, or disabled-account credential — one generic body (the specific cause is logged server-side, never returned). Carries `WWW-Authenticate: Bearer realm="api"`; an invalid cookie is also cleared. |
| `401` | `invalid_credentials` | Login / password change with a wrong password (or unknown email). |
| `403` | `origin_not_allowed` | Cookie-authenticated `POST/PUT/PATCH/DELETE` (or a login/register from a browser) whose `Origin` is missing or not allowed. |
| `403` | `account_disabled` | Correct password, disabled account. |
| `404` | `not_found` | Unchanged: another user's id, or none. Never 403. |
| `429` | `rate_limited` | With `Retry-After: <seconds>` and `details.limit_class`. |

## Browser security boundary (CSRF, CORS)

* **CSRF:** a cookie-authenticated state-changing request must carry an
  `Origin` that is an explicitly allowed origin (`FRONTEND_URL`, `CORS_ORIGINS`),
  the configured Vercel project's preview, or this API's own origin — otherwise
  `403`. `SameSite=Lax` is the second layer. Bearer requests are exempt (not
  ambient credentials). No CSRF tokens are needed
  ([ADR 0022](adr/0022-cookie-origin-policy-and-same-site-topology.md)).
* **CORS:** credentials are allowed only for explicit origins. The pre-Phase-9
  default that admitted **every** `*.vercel.app` site was removed;
  `CORS_ALLOW_VERCEL_PREVIEWS=true` now requires `CORS_VERCEL_PROJECT=<slug>`
  and admits only `https://<slug>(-…)?.vercel.app`. Allowed request headers are
  `Authorization`, `Content-Type`, `X-Request-ID` (plus `X-User-Id` in
  dev-header mode only).
* **Topology:** the Phase 10 frontend is served same-site through a Next.js
  same-origin proxy (approved Q3), so the host-only `SameSite=Lax` cookie works
  and browsers' third-party-cookie blocking does not apply.

## Rate limits

PostgreSQL fixed-window counters, each hit committed in its **own**
transaction — so a request that fails (e.g. a wrong-password `401`) still
counts ([ADR 0023](adr/0023-postgresql-rate-limiting.md)). Limits are per
window; the second request past a limit gets `429`.

| Class | Endpoints | Keyed by | Default |
|---|---|---|---|
| `auth_login_ip` + `auth_login_email` | `POST /auth/login`, `POST /auth/password` | client IP **and** SHA-256(email); both counted on every attempt | 10 / 15 min per IP; 5 / 15 min per email |
| `auth_register` | `POST /auth/register` | client IP | 5 / hour |
| `graph` | `/graph/*`, `GET /entities/{id}/graph` | user | 120 / min |
| `search` | `POST /search`, `GET /memories/search`, `GET /entities/search` | user | 120 / min |
| `agent` | `POST /conversations/{id}/agent-turn` | user | 30 / hour |
| `message` | `POST /conversations/{id}/messages` | user | 120 / hour |
| `ingest` | `POST /documents` | user | 20 / hour |
| `write` | `POST /memories`, `PATCH /memories/{id}`, `POST /memories/{id}/invalidate`, review `POST`s | user | 120 / min |

User classes run **after** authentication (an anonymous flood gets cheap `401`s
and never consumes a user's quota) and **before** body validation (an invalid
request still counts). The Phase 8 per-request graph budgets are unchanged and
remain the hard ceiling on work per request.

**Client IP:** the direct peer address. `X-Forwarded-For` is consulted ONLY
when the peer is in `TRUSTED_PROXY_IPS` (IPs/CIDRs), taking the right-most
non-proxy hop; from any other peer it is ignored, so a client cannot choose its
own bucket. Behind the Next.js proxy, set `TRUSTED_PROXY_IPS` to the proxy's
address or every client shares one per-IP bucket (the per-email limit does not
depend on IP).

## Development and test identity

`AUTH_MODE=dev_header` (or the deprecated `ALLOW_USER_ID_HEADER=true`)
restores the pre-Phase-9 placeholder: identity from `X-User-Id`, falling back
to `DEV_USER_ID`. A presented session credential still wins. Startup **fails**
in any `APP_ENV` other than `development`/`test` if it is selected, and also if
cookies are not `Secure`, no browser origin is configured, or the scrypt cost
is below the OWASP floor. The pre-Phase-9 test suite runs in dev-header mode;
Phase 9 tests run in session mode.

## Operator CLI

`scripts/manage_users.py` (run from `backend/`; talks to PostgreSQL directly,
no HTTP): `create --email E [--user-id ID]`, `disable`, `enable`,
`reset-password`, `revoke-sessions`. Passwords are read from a no-echo prompt
or one stdin line (`--password-stdin`), **never** from arguments.
`create --user-id dev-user` / `demo-user` attaches a login to an **existing**
ownership id without touching its data; it refuses ids with no existing data
and ids that already have an account (so it cannot reassign ownership).
Disabling revokes every session immediately.

---

# 4. Endpoints

Every endpoint except `/health*` and `/auth/{register,login,logout}` requires a
session credential (section 3a). Every response carries `X-Request-ID`. Live
OpenAPI schema at `/docs` (public; schema only, no user data).

## Memories

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/memories` | Create a memory with entities, relationships, source, supersessions |
| `GET` | `/memories/search` | Keyword + attribute + temporal search |
| `GET` | `/memories/{id}` | One memory with everything one hop away |
| `PATCH` | `/memories/{id}` | Update **metadata only** |
| `POST` | `/memories/{id}/invalidate` | Retract a memory |

**There is no `DELETE`.** Memory is append-only: retraction is recorded, not erased,
and a `DELETE` verb would imply the data is gone.

**`PATCH` cannot change `content`.** Rewriting stored text in place would destroy the
historical record with no trace. Express a changed fact by creating a new memory whose
`supersedes` names the old one. `PATCH` also refuses `status: invalid` (use the
invalidate endpoint, which also closes the asserted edges) and `status: superseded`
(create the replacement instead).

### `POST /memories`

```jsonc
{
  "content": "The Drawing Extractor project uses EasyOCR for text extraction.",
  "memory_type": "semantic",              // default: semantic
  "status": "active",                     // active | historical | conflicting
  "confidence": 1.0,
  "entities": [
    {"name": "Drawing Extractor", "entity_type": "project"},
    {"name": "EasyOCR", "entity_type": "technology", "aliases": ["easy ocr"]}
  ],
  "relationships": [
    {"source": "Drawing Extractor", "predicate": "uses", "target": "EasyOCR"}
  ],
  "source": {"source_type": "conversation", "reference": "conversation-101"},
  "valid_from": "2026-01-15T09:00:00Z",   // defaults to now
  "valid_until": null,
  "supersedes": []                        // memory ids this one replaces
}
```

Returns `201` with the stored memory, **read back from the graph** rather than
assembled optimistically. The whole write is one transaction: if any id in
`supersedes` does not exist, nothing is written at all.

Relationship endpoints must also appear in `entities`, so a typo cannot silently
create a new entity.

### `GET /memories/search`

| Param | Notes |
|---|---|
| `q` | Case-insensitive substring of `content` |
| `status` | Repeatable. Default: all but `invalid`/`archived` |
| `memory_type` | Repeatable |
| `entity` | Entity name; returns memories mentioning it |
| `as_of` | ISO datetime; only memories whose window contains it |
| `include_excluded` | Include `invalid` and `archived` |
| `limit` / `offset` | `limit` capped by `SEARCH_MAX_LIMIT` |

## Entities

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/entities/search?q=` | Substring search over names and aliases. Merged-away entities are hidden unless `include_merged=true` (Phase 8, D2); their names remain searchable as aliases of the canonical entity and their ids still resolve |
| `GET` | `/entities/by-name?name=` | Exact resolution by normalized name |
| `GET` | `/entities/{id}` | Entity with memory and relationship counts |
| `GET` | `/entities/{id}/relationships` | Incident edges (`direction`, `as_of`) |
| `GET` | `/entities/{id}/memories` | Memories mentioning the entity |
| `GET` | `/entities/{id}/history` | **Current vs historical state** (`as_of`) |
| `GET` | `/entities/{id}/graph?hops=N` | N-hop subgraph. Depth bounded by `GRAPH_MAX_HOPS`; result bounded by `GRAPH_MAX_NODES` / `GRAPH_MAX_EDGES` (Phase 8, D1), with `truncated: true` when a cap cut it. No temporal filter: superseded edges are included. For a Memory Explorer use `/graph/*` (section 4c) instead |

### `GET /entities/{id}/history`

The `currently_uses` / `previously_used` projection, derived from one predicate:

* **`current`** — the window contains `as_of`
* **`historical`** — the window closed at or before `as_of`
* an edge whose window starts *after* `as_of` appears in neither

`as_of` defaults to now. Passing a past instant returns the state as it was then:

```
GET /entities/{id}/history                       → current: PaddleOCR, Gemini
                                                   historical: EasyOCR
GET /entities/{id}/history?as_of=2026-03-01T00:00:00Z
                                                 → current: EasyOCR, Gemini
                                                   historical: (none)
```

## Health

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | **Liveness.** Never touches the database. |
| `GET` | `/health/ready` | **Readiness.** `503` when Neo4j is unreachable. |

Split deliberately: a graph outage must not make the platform kill and restart a
healthy process.

## Errors

Every error has a stable machine-readable `code`:

```json
{
  "error": {
    "code": "not_found",
    "message": "Memory not found",
    "details": {"memory_id": "..."},
    "request_id": "8429a351-..."
  }
}
```

| Code | HTTP | Meaning |
|---|---|---|
| `request_validation_error` | 422 | Schema violation; `details.fields` lists them |
| `validation_error` | 422 | Domain rule rejected the input |
| `not_found` | 404 | No such record **for this user** |
| `conflict` | 409 | Illegal status transition, or constraint violation |
| `unauthorized` | 401 | Missing/invalid `X-Internal-Token` (internal endpoints only) |
| `graph_unavailable` | 503 | Neo4j unreachable |
| `graph_query_error` | 500 | Query failed; details logged, not returned |
| `database_unavailable` | 503 | PostgreSQL unreachable |
| `database_error` | 500 | SQL query failed; details logged, not returned |
| `configuration_error` | 500 | Missing or invalid configuration |
| `internal_error` | 500 | Unexpected; traceback logged against `request_id` |

`llm_unavailable` and `llm_response_invalid` are recorded on `extraction_jobs.error_code`
(visible via `GET /extraction/jobs/{id}`), not raised as HTTP errors — an LLM
failure is asynchronous and never reaches a request (docs/adr/0008).

---

# 4a. Conversation, extraction and provenance endpoints (Phase 2)

All require the same session credential as every other user route (section 3a), except the internal router (operator `X-Internal-Token`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/conversations` | Create a conversation |
| `GET` | `/conversations` | List conversations |
| `GET` | `/conversations/{id}` | Conversation with its messages |
| `POST` | `/conversations/{id}/messages` | Append a message; runs the gate synchronously, schedules extraction as a background task if it passes |
| `GET` | `/conversations/{id}/messages` | List messages, in order |
| `POST` | `/conversations/{id}/agent-turn` | Phase 5: append the user's message, run the persistent-memory agent, append and return its answer |
| `GET` | `/extraction/jobs` | List extraction jobs for the user |
| `GET` | `/extraction/jobs/{id}` | One job's status plus every candidate it produced |
| `GET` | `/memories/{id}/provenance` | Resolve a memory's sources back to the messages that produced them |
| `POST` | `/internal/extraction/sweep` | **Not user-scoped.** Guarded by `X-Internal-Token` (`INTERNAL_API_TOKEN`). Runs one bounded sweep tick — intended for a platform cron, not end users. |

### `POST /conversations/{id}/messages`

```jsonc
{"role": "user", "content": "The Drawing Extractor project uses Gemini."}
```

Returns `201` with the stored message, including `extraction_status`:
`pending` (extraction disabled), `queued` (gate passed, background task scheduled),
or `skipped` (gate declined — `extraction_gate_reason` explains why).

### `GET /extraction/jobs/{id}`

```jsonc
{
  "job": {
    "id": "...", "status": "done", "attempts": 1,
    "provider": "gemini", "model": "...", "prompt_version": "v1",
    "input_tokens": 412, "output_tokens": 96, "latency_ms": 830,
    "candidate_count": 1, "error_code": null, "error_detail": null
  },
  "candidates": [
    {
      "decision": "SUPERSEDING",
      "decision_reason": "replaces_hint matched the existing target 'easyocr'",
      "memory_id": "...",
      "payload": { "...": "the full validated candidate, for replay/debugging" }
    }
  ]
}
```

This is the audit trail between "the LLM proposed" and "the engine decided" —
`decision_reason` is human-readable, and `payload` is the complete validated
candidate, so a surprising result can be replayed and understood without re-calling
the model.

### `GET /memories/{id}/provenance`

```jsonc
{
  "memory": { "id": "...", "content": "Drawing Extractor uses Gemini", "...": "..." },
  "sources": [
    {
      "source": {"source_type": "message", "reference": "<message_id>", "location": "12:45"},
      "message": {"content": "The Drawing Extractor project uses Gemini.", "conversation_id": "..."}
    }
  ]
}
```

`message` is `null` when the source isn't a message (Phase 1 memories, or a message
that no longer resolves) — this is the one place the API deliberately crosses the
PostgreSQL/Neo4j boundary in a read.

### `POST /internal/extraction/sweep`

```bash
curl -X POST $BACKEND_URL/internal/extraction/sweep -H "X-Internal-Token: $INTERNAL_API_TOKEN"
```

```jsonc
{"reclaimed_leases": 0, "rechecked_skipped": 2, "jobs_processed": 5}
```

Bounded and idempotent (docs/adr/0008) — safe to call on any schedule. Returns
`401` without a valid token, `500 configuration_error` if `INTERNAL_API_TOKEN` is
unset (the endpoint fails closed, not open).

---

# 4b. Hybrid search and embedding backfill (Phase 3)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/search` | Hybrid graph + vector + keyword search |
| `POST` | `/internal/embeddings/backfill` | **Not user-scoped.** Guarded by `X-Internal-Token`. Embeds any memory lacking a row under the currently-configured model, for a bounded page of users. |

### `POST /search`

```jsonc
{"query": "What does Drawing Extractor currently use for text recognition?"}
```

```jsonc
{
  "items": [
    {
      "memory": { "memory": {"id": "...", "content": "Drawing Extractor uses PaddleOCR..."}, "...": "..." },
      "explanation": {
        "memory_id": "...",
        "retrieved_via": ["vector", "graph"],
        "vector_similarity": 0.91,
        "graph_relevance": 1.0,
        "keyword_match": null,
        "temporal_fit": 1.0,
        "confidence": 1.0,
        "final_score": 2.71,
        "temporal_state": "current"
      }
    }
  ]
}
```

A `null` field in `explanation` means that channel never found this result at
all — never a fabricated zero (ADR 0009). The query's temporal intent (`now`/
`current`, `before`/`previously`/`used to`, an explicit `in <Month>`, or none of
those) is detected deterministically (`app/semantic/query_understanding.py`, no
LLM) and applied as a hard filter against Neo4j's authoritative status/validity —
never decided by embedding similarity. If PostgreSQL/pgvector is unavailable,
the vector channel is silently dropped and graph/keyword results are still
returned — a vector-index outage never makes a memory appear nonexistent.

### `POST /internal/embeddings/backfill`

```bash
curl -X POST "$BACKEND_URL/internal/embeddings/backfill?max_users=100" \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN"
```

```jsonc
{"users_processed": 3, "scanned": 12, "embedded": 4, "skipped_already_present": 8, "failed": 0}
```

Resumable, idempotent, and bounded per call (`max_users`, and per-user
`EMBEDDING_BACKFILL_BATCH_SIZE`) — safe to call on any schedule, same shape as
the extraction sweep. Loops over a page of known users because Neo4j's
`$user_id` scoping means there is no single "all users" query, unlike the
PostgreSQL-only extraction claim step.

## Review workflow (Phase 4, ADR 0010, PHASE4_PLAN.md sections 6/9-12/15)

Every endpoint below is user-scoped through the authenticated session like the
rest of the API. Write endpoints require `if_version` (optimistic concurrency): a stale
value is rejected with `409 conflict` naming the current version, never a
silent overwrite. Repeating an already-applied action with the same or a
stale `if_version` is idempotent — it returns the existing state rather than
erroring or re-mutating.

### `GET /review/memories` / `GET /review/memories/{id}`

Lists pending `likely_duplicate` / `flagged` / `conflicting` candidates (list
is paginated, `unreviewed` only); the detail view resolves both memories in
full (`memory_a`/`memory_b`), plus the recorded `evidence` and `score`.

### `GET /review/entities` / `GET /review/entities/{id}`

Lists pending entity-merge candidates. The detail view resolves
`entity_a`/`entity_b` — either may be `null` if it was flagged via a
`pending:<name>` placeholder (the candidate's own entity did not exist in
Neo4j yet at flag time) and still does not exist; once it is created, the
same detail call resolves it by name automatically.

### `GET /review/history`

The append-only audit trail (`action`, `reviewer_id`, `previous_state`,
`resulting_state`, `reason`, `created_at`) — never itself editable.

### `POST /review/memories/{id}/confirm-duplicate`

```jsonc
{"if_version": 1, "reason": "near-identical restatement"}
```

`memory_id_a` (the newer memory) is marked `MERGED` with a `DUPLICATE_OF`
edge to `memory_id_b` (kept as canonical). Neither memory is deleted; both
remain directly resolvable by id, and `GET /memories/{id}` reports
`duplicate_of`/`duplicates`.

### `POST /review/memories/{id}/confirm-distinct`

```jsonc
{"if_version": 1, "reason": "unrelated facts"}
```

No Neo4j mutation — `LIKELY_DUPLICATE`/`FLAGGED` never touched Neo4j in the
first place (ADR 0009), so there is nothing to undo.

### `POST /review/memories/{id}/resolve-conflict`

```jsonc
{"if_version": 1, "resolution": "supersede", "winner_memory_id": "<id>", "reason": "..."}
```

`resolution` is one of `supersede` (requires `winner_memory_id`, one of the
candidate's own two memories — the loser is superseded exactly like any other
supersession), `keep_both` (no mutation; the conflict is acknowledged, not
eliminated), or `reject` (the flagged memory returns to `ACTIVE`, nothing
superseded). The `CONFLICTS_WITH` edge itself is never removed by any
resolution — it stays as a historical record that a conflict was flagged.

### `POST /review/entities/{id}/approve-merge`

```jsonc
{"if_version": 1, "canonical_entity_id": "<id>", "reason": "same company"}
```

`canonical_entity_id` must be one of the candidate's own two entities — the
reviewer picks which one survives, never inferred automatically. The other
entity is never deleted: its relationships are copied (not moved) onto the
canonical entity, and a `MERGED_INTO` redirect is created so lookups by its
id resolve to the canonical one (`EntityRepository.resolve`, the one
canonical resolution mechanism every entity-retrieval path uses).

### `POST /review/entities/{id}/reject-merge`

```jsonc
{"if_version": 1, "reason": "distinct companies"}
```

No Neo4j mutation — both entities stay distinct.

### `POST /review/entities/{id}/reverse-merge`

```jsonc
{"if_version": 1, "reason": "correction"}
```

Undoes a previously-approved merge: the copied relationships (tagged by the
original merge's id) are removed and the redirect is deleted. The merged
entity's alias addition is **not** retracted — a documented, accepted
limitation (it may have accumulated other aliases since).

### `POST /internal/review/reconcile`

```bash
curl -X POST "$BACKEND_URL/internal/review/reconcile?max_users=100" \
  -H "X-Internal-Token: $INTERNAL_API_TOKEN"
```

```jsonc
{"users_processed": 3, "memory_duplicates_checked": 5, "memory_duplicates_repaired": 0,
 "entity_merges_checked": 2, "entity_merges_repaired": 0}
```

Re-checks every `confirmed_duplicate`/`merged` PostgreSQL row against Neo4j's
actual current state, repairing the narrow crash window between "Neo4j
canonical mutation" and "PostgreSQL review-state update" (no distributed
transaction spans the two) by re-applying the same idempotent Neo4j write —
never a freshly-guessed outcome. Same bounded-page-of-users shape as the
embedding backfill sweep.

---

# 4c. Memory graph exploration API (Phase 8)

A bounded, **read-only** contract for a Memory Explorer UI
(`docs/PHASE8_PLAN.md`). Every endpoint is `GET` and user-scoped through the
same authenticated identity as the rest of the API (section 3a) and rate-limited
as class `graph`. No endpoint accepts Cypher, labels, relationship types or
free-form filter expressions, and none writes to either store.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/graph/overview` | Bounded landing slice |
| `GET` | `/graph/entities/{entity_id}/neighborhood` | Bounded, incremental expansion around one entity |
| `GET` | `/graph/edges/evidence` | One relationship → its asserting memories → their provenance |
| `GET` | `/graph/memories/{memory_id}/timeline` | One memory's supersession / duplicate / conflict chain |

## The `GraphSlice` response

`overview`, `neighborhood` and `timeline` return one shape:

```json
{
  "root": "<entity or memory id | null for overview>",
  "resolved_from": "<the id asked for, if a MERGED_INTO redirect was followed | null>",
  "redirect_depth": 0,
  "temporal_scope": "current | as_of | all",
  "as_of": "<the instant temporal_state was computed at>",
  "budget": {"max_nodes": 150, "max_edges": 400, "max_hops": 1, "max_memories_per_entity": null},
  "truncated_nodes": false,
  "truncated_edges": false,
  "truncated_memories": false,
  "nodes": [{"id": "...", "kind": "entity | memory | source", "label": "...", "...": "..."}],
  "edges": [{"id": "...", "kind": "relates_to | mentions | supersedes | conflicts_with | duplicate_of | derived_from",
             "source": "...", "target": "...", "...": "..."}]
}
```

* **Complete vs partial is always explicit.** `truncated_nodes` /
  `truncated_edges` are exact: the server fetches one more than the budget,
  so `false` means nothing in scope was left out. `truncated_memories` is
  set when `include=memories` hit the per-entity cap or the node budget.
  There are no totals; counting a whole neighbourhood would defeat the
  budget.
* **`budget` is the effective, post-clamp bound.** A client may lower any
  budget per request; a request above the server cap is clamped, never
  honoured.
* **Entity nodes** carry `entity_type`, `aliases`, `hops` (BFS distance from
  the root, neighborhood only) and, with `include=merges`,
  `merged_from_count`. **Memory nodes** carry `memory_type`, `status`,
  `confidence`, `valid_from`/`valid_until`, `temporal_state`, and a `label`
  truncated to 200 characters (the full text is `GET /memories/{id}`).
* **`relates_to` edges are aggregated.** Several stored `RELATES_TO` edges
  for the same `(source, predicate, target, status, temporal_state)` become
  one slice edge. It carries `assertion_count` and up to 20 `memory_ids`;
  the full list is the evidence endpoint's job. Storage is unchanged
  (ADR 0001): each assertion remains its own edge. Edge ids are stable and
  deterministic.
* **No layout coordinates.** Layout is a UI concern.
* **Rendering contract.** Every string (entity names, memory content,
  message and document text) is plain, untrusted user data. A client must
  render it as text, never as HTML.

## Temporal scope

Temporal meaning is computed on the server with the same half-open
`[valid_from, valid_until)` semantics as `app/models/lifecycle.py`. A null
`valid_from` means "since the beginning", and a null `valid_until` means "no
known end". Status and window filters are applied **inside the traversal, on
every hop**, never after a limit.

| `temporal_scope` | Includes | `as_of` |
|---|---|---|
| `current` (**default**, D3) | status `active` or `conflicting`, and window contains now | must be omitted (422 otherwise) |
| `as_of` | any status except `invalid`/`archived`/`merged`, window contains `as_of` | **required** |
| `all` | any status except `invalid`/`archived`/`merged`, regardless of window | optional: moves the instant `temporal_state` is computed against (default now) |

`temporal_state` on every edge and memory node is `current`, `historical`
(the window closed at or before `as_of`) or `not_yet_valid` (the window
starts after `as_of`). `as_of` must carry a timezone offset (`...Z`); a naive
instant is rejected with 422.

A superseded edge that was valid at `T` **is** included under
`as_of=T`, because it was true then. It keeps `status: "superseded"` and is
`temporal_state: "current"` relative to `T`. For an entity's one-hop
facts this is exactly `GET /entities/{id}/history?as_of=T`'s `current`
bucket (cross-checked by tests). Window boundaries are compared by instant,
so a fact whose window ends at `T` is excluded at `T` and included one
microsecond earlier.

Historical facts are never traversed in `current` scope. A node reachable
only through a superseded edge does not appear.

## Identity: merged entities and duplicates

* An `entity_id` that was merged away resolves through the existing
  `MERGED_INTO` resolver: merge-chain safe, cycle-safe, user-scoped. The
  slice is rooted at the canonical entity, and `resolved_from` /
  `redirect_depth` report the redirect.
* Merged-away entities never appear as nodes in any slice. Their
  relationships were copied onto the canonical entity at merge time, and
  those copies are what the slice shows.
* `MERGED` (confirmed duplicate) memories are excluded, and so is any
  `RELATES_TO` edge whose asserting memory is `merged`, `invalid` or
  `archived`. A duplicate's own edge keeps status `active` (ADR 0010), so
  without this rule it would inflate `assertion_count`.

## Isolation

Every node and every relationship in every query is filtered on the
requesting user. Another user's ids behave exactly like ids that do not
exist:

* path ids, `source_entity_id` / `target_entity_id` and `memory_id` return
  **404** (never 403);
* ids in `exclude_ids` have **no effect**: they are never matched;
* `overview` returns an empty slice.

## `GET /graph/overview`

The user's canonical entities, **most recently updated first**, with ties
broken by name and then id (deterministic). Merged-away entities are
excluded. The response also includes the in-scope edges among the returned
entities.

This is the plan's approved fallback ordering (`docs/PHASE8_PLAN.md` section
9.1). The planned "rank by in-scope degree" was measured at about 3.8 s warm
(5.5M db hits) at 10k synthetic entities, against about 62 ms for this
ordering. A slice's *edges* are in-scope-filtered either way; only the choice
of which entities to show changes. An entity with no in-scope relationship
can therefore appear with no edges.

| Param | Default | Bounds |
|---|---|---|
| `limit` | `GRAPH_EXPLORE_MAX_NODES` | clamped to it |
| `max_edges` | `GRAPH_EXPLORE_MAX_EDGES` | clamped to it |
| `temporal_scope`, `as_of` | `current` | see above |
| `entity_type` | all | repeatable `EntityType` value |
| `include` | none | repeatable: `memories`, `merges` |

## `GET /graph/entities/{entity_id}/neighborhood`

A breadth-first expansion, one bounded query per hop. There is no
variable-length path pattern, so no value is ever interpolated into Cypher.
The node budget counts the root. Nodes are ordered by `(hops, name, id)`,
so a truncated slice is always the same set. If the budget is exactly full
at a hop boundary, the server probes the next hop for one more in-scope node
so that `truncated_nodes` stays exact.

| Param | Default | Bounds |
|---|---|---|
| `hops` | `1` | `1..GRAPH_EXPLORE_MAX_HOPS` (422 above) |
| `max_nodes` / `max_edges` | settings | clamped to settings |
| `temporal_scope`, `as_of` | `current` | see above |
| `predicate` | all | repeatable, each `^[a-z][a-z0-9_]{0,62}[a-z0-9]$`, at most 20 |
| `entity_type` | all | repeatable `EntityType` value; a non-matching node is neither returned nor expanded through |
| `exclude_ids` | none | repeatable id, at most `GRAPH_EXPLORE_MAX_NODES` |
| `include` | none | `memories`, `merges` |

**Incremental expansion.** Pass the ids already on screen as `exclude_ids`.
Excluded nodes are neither returned nor re-expanded, but edges from new
nodes back into them are returned so the client can connect the two.

`include=memories` adds, for every entity node, up to
`GRAPH_EXPLORE_MAX_MEMORIES_PER_ENTITY` in-scope memories that mention it
(newest first), all within the node budget. It also adds `mentions` edges
and the `supersedes` / `conflicts_with` / `duplicate_of` links between the
memories in the slice. A relationship-less ("bare") memory is visible only
this way.

## `GET /graph/edges/evidence`

"Why does the system believe `X -[predicate]-> Y`?" in one call. It returns
every stored `RELATES_TO` edge for that directed triple, with its asserting
memory and that memory's resolved provenance. It replaces the old N+1
sequence: one `GET /memories/{id}` plus one `GET /memories/{id}/provenance`
per memory.

| Param | Default | Bounds |
|---|---|---|
| `source_entity_id`, `target_entity_id` | required | merged ids resolve; another user's id → 404 |
| `predicate` | required | predicate pattern, ≤ 100 chars |
| `temporal_scope`, `as_of` | **`current`** (D3) | pass `all` to include superseded assertions |
| `limit` | `SEARCH_MAX_LIMIT` | clamped to it; `truncated` is exact |

Each `assertions[]` item has the edge's `status`, window and
`temporal_state`, plus `memory_id`, `evidence_status` and `memory`. The
`memory` field holds content, status, window, link fields and `sources[]`;
each source is resolved to a message (role plus content, ≤ 4,000 chars) or to
a document plus chunk (filename, chunk content, page, section).
`nodes`/`edges` repeat the memory and source nodes with `derived_from`
edges, for drawing.

Evidence that cannot be shown is labelled, never fabricated:

| `evidence_status` | Meaning |
|---|---|
| `available` | the memory exists and at least one source resolved |
| `no_asserting_memory` | the edge has no `memory_id` (e.g. legacy or seeded data) |
| `memory_not_found` | `memory_id` points at nothing readable for this user |
| `no_sources` | the memory has no `DERIVED_FROM` source |
| `sources_unresolved` | sources exist but none resolved (e.g. the message or document was deleted); `resolved: false` per source, metadata only |

`evidence_available` is true iff at least one assertion is `available`.

Cost is constant in the number of memories and sources. Each request makes
three Neo4j reads (two entity resolutions and one evidence query) and at
most three SQL statements (messages, documents, chunks, batched). This is
measured in `eval/graph_api_benchmark.py` and pinned by a test.

## `GET /graph/memories/{memory_id}/timeline`

The memory's `supersedes` / `superseded_by` / `duplicate_of` / `duplicates` /
`conflicts_with` chain. It is produced by the same walk as the agent's
`get_memory_history` tool (`app/services/memory_history.py`), which here
additionally follows `conflicts_with`. It is bounded at 20 memories, and
`truncated_nodes` is set if more remained.

* Nodes are **oldest first**: `valid_from`, then `created_at`, then `id`.
* `status` is reported exactly as stored. `superseded`, `merged`, `invalid`,
  `conflicting` and `historical` are never collapsed or re-derived.
  Superseding a `historical` memory re-files it as `superseded`, and the
  timeline shows that.
* `temporal_scope` is always `all`: a timeline is an explicit request for
  history.
* Edges: `supersedes` (newer → older), `duplicate_of` (duplicate →
  canonical) and `conflicts_with` (one edge per pair, lower id first), between
  chain members only. `budget.max_edges` is `null` because edges are bounded
  by the bounded node set.

## Errors

422 for invalid parameters (`hops` out of range, `as_of` missing, forbidden
or naive, a malformed id, predicate, entity type or `include` value, too many
`exclude_ids` or predicates). 404 for an unknown or other-user id. Mutating
methods return 405.

---

# 5. Running locally

No Docker required.

## Prerequisites

* Python 3.12+ (developed on 3.14)
* [`uv`](https://docs.astral.sh/uv/)
* A Neo4j instance — either free managed **Aura**, or local:
  ```bash
  brew install neo4j
  neo4j-admin dbms set-initial-password <password>   # before first start
  brew services start neo4j
  ```
* A PostgreSQL instance (Phase 2/3) — either free managed **Supabase/Neon**, or
  local. Must support installing the `vector` extension (standard on all of the
  above; the Phase 3 migration installs it, no manual step needed):
  ```bash
  brew install postgresql@17 && brew services start postgresql@17
  createdb ai_second_brain
  ```

## Setup

```bash
cp .env.example .env     # set NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD and DATABASE_URL
cd backend
uv venv && uv pip install -e ".[dev]"
```

`NEO4J_URI` is `bolt://localhost:7687` locally, or
`neo4j+s://<id>.databases.neo4j.io` for Aura. `DATABASE_URL` is
`postgresql+asyncpg://localhost/ai_second_brain` locally, or the connection string
Supabase/Neon gives you (with `+asyncpg` after `postgresql`). Leave `LLM_PROVIDER`
and `EMBEDDING_PROVIDER` unset or `fake` to run everything — extraction, semantic
duplicate detection, and hybrid search — with no API key.

## Apply the schema

```bash
cd backend
uv run python ../scripts/init_graph.py    # Neo4j: constraints and indexes
uv run alembic upgrade head               # PostgreSQL: conversations, messages,
                                           # extraction_jobs, extraction_candidates,
                                           # the `vector` extension, memory_embeddings
```

Both are idempotent — safe to re-run. `init_graph.py` reports every statement and
fails loudly; Alembic tracks applied migrations in `alembic_version`. If
`EMBEDDING_DIMENSION` is ever changed, a new migration is required — the app
refuses to start on a mismatch between that setting and the deployed column
width rather than risk a silent corruption.

## Run the API

```bash
cd backend && uv run uvicorn app.main:app --reload --port 8000
```

Then `http://localhost:8000/docs`. The app never binds a port itself; managed hosts
supply `$PORT` (see `backend/Procfile`).

**Authentication (Phase 9).** Apply migrations first (`uv run alembic upgrade
head` adds `user_accounts`, `auth_sessions`, `rate_limit_counters`), then either:

```bash
# session mode (the default): create an account, then log in
uv run python ../scripts/manage_users.py create --email you@example.com
# ...or give the seeded demo data a login, keeping its ownership id:
uv run python ../scripts/manage_users.py create --email demo@example.com --user-id demo-user
curl -s -X POST localhost:8000/auth/login -H 'content-type: application/json' \
     -d '{"email":"you@example.com","password":"...","token_in_body":true}'
# then send: Authorization: Bearer <token>
```

or keep the pre-Phase-9 workflow for quick local poking with
`AUTH_MODE=dev_header` (never valid outside development/test).

## Seed the demo data

```bash
cd backend && uv run python ../scripts/seed_demo.py           # add
cd backend && uv run python ../scripts/seed_demo.py --reset   # wipe first
```

Writes the documented narrative under user `demo-user`:

```
January   Drawing Extractor --uses--> EasyOCR
          Drawing Extractor --uses--> Gemini
          EasyOCR --related_to--> OCR          (gives the graph a second hop)
May       Drawing Extractor --uses--> PaddleOCR, superseding the EasyOCR memory
```

Then asserts that the EasyOCR memory still exists as `superseded` with a closed
window, and prints current vs historical state plus the 2-hop neighbourhood.

`--reset` deletes the seed user's nodes. It lives in a script, not the API, because
the API is append-only.

## Run the tests

```bash
cd backend

uv run pytest tests/unit -q          # 383 tests, no database needed
uv run pytest -q                     # all tests; integration skips without services

NEO4J_URI=bolt://localhost:7687 NEO4J_USERNAME=neo4j NEO4J_PASSWORD=<pw> \
DATABASE_URL=postgresql+asyncpg://localhost/ai_second_brain_test \
  uv run pytest -q                   # all 494, including integration

uv run pytest -m integration -q      # integration only (107 tests)
uv run ruff check app tests ../scripts eval
```

Integration tests skip automatically with a printed reason when Neo4j or PostgreSQL
is unreachable (independently — each store's tests skip on their own), so `pytest`
works on a machine with only one, or neither, installed. Each integration test uses
unique ids and cleans up after itself (both stores), so they neither collide nor
depend on order. Use a **separate test database** (`ai_second_brain_test`), migrated
the same way as the dev one — the suite writes and deletes real rows.

## Evaluation harness (Phase 2)

```bash
cd backend
uv run python eval/runner.py
```

Runs the 10-scenario gold dataset (`eval/datasets/extraction_v1.jsonl`) through the
real pipeline — `ExtractionRunner`, real Neo4j, real PostgreSQL — using
`FakeLLMClient` scripted to return each scenario's expected candidates. This measures
whether the **deterministic** layers (grounding, decision engine, persistence) reach
the right outcome given a correct extraction; it does not measure whether a real
model would have produced that extraction in the first place — see
`eval/runner.py`'s docstring and docs/PHASE2_PLAN.md section 11.

```bash
cd backend
uv run python eval/retrieval_runner.py
```

Runs the retrieval gold dataset (`eval/datasets/retrieval_v1.jsonl`) through
`HybridRetrievalService` against real Neo4j and real PostgreSQL, using
`FakeEmbeddingClient`. Reports Recall@K, Precision@K and MRR for three
configurations per scenario — graph-only, vector-only, and hybrid — so the
value each channel adds is a measured number, not an assumption (ADR 0009,
docs/PHASE3_PLAN.md section 16). Like `eval/runner.py`, this measures retrieval
correctness, not answer quality (no agent exists yet to generate an answer).

## Continuous integration

Two workflows, deliberately separate:

| Workflow | Trigger | Needs services | Runs |
|---|---|---|---|
| `.github/workflows/ci.yml` | every push + PR | **No** | Ruff, 484 unit tests, Cypher/SQL safety audits, skip guard |
| `.github/workflows/integration.yml` | manual + weekly cron | **Yes** | Schema apply (Neo4j + Alembic) + 193 integration tests |

The default pipeline requires no database and no Docker, so every push gets fast,
free, deterministic feedback. Unit tests run against Python **3.12** (the declared
floor) and **3.14** (the development version), which catches accidental use of
newer-only syntax.

The Cypher and SQL safety audits are called out as their own CI step rather than
left inside the unit run, because a failure there is a potential cross-user data
leak rather than a style problem. A further "skip guard" step asserts that
integration tests still skip cleanly without services — otherwise they could
quietly stop covering the graph and the database while CI stayed green.

The integration workflow connects to **managed Neo4j Aura and managed PostgreSQL**
through repository secrets (`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`,
`DATABASE_URL`), which matches the production target and needs no Docker on the
runner. With secrets absent it exits successfully with a notice; with secrets
present it *fails* if tests skip, so a misconfiguration cannot masquerade as a pass.
Point it at dedicated test instances: the suite writes and deletes data.

---

# 6. Environment variables

See `.env.example` for the full annotated list. Required regardless of phase:

| Variable | Notes |
|---|---|
| `NEO4J_URI` | **Required.** No default, by design. |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | **Required.** |
| `NEO4J_DATABASE` | Default `neo4j` |
| `DATABASE_URL` | **Required as of Phase 2.** No default, by design. |
| `FRONTEND_URL` | Origin for the CORS allow-list |
| `CORS_ORIGINS` | Extra origins, comma-separated |
| `CORS_ALLOW_VERCEL_PREVIEWS` | Default `true`; admits `https://*.vercel.app` |
| `GRAPH_MAX_HOPS` | Default `4`; caps traversal depth |
| `SEARCH_MAX_LIMIT` | Default `200` |
| `DEV_USER_ID` | Fallback identity in `AUTH_MODE=dev_header` only |
| `ALLOW_USER_ID_HEADER` | Deprecated alias of `AUTH_MODE=dev_header`; refused in any production-like `APP_ENV` |
| `NEO4J_MAX_TRANSACTION_RETRY_TIME` | Default `5.0s`; the driver default of 30s would hang requests during an outage |

## Phase 2 additions

| Variable | Notes |
|---|---|
| `LLM_PROVIDER` | Default `fake` — the whole system, including extraction, runs with no API key. Set `gemini` for real extraction. |
| `LLM_MODEL` | **Pin the exact model id here, never in code.** Required when `LLM_PROVIDER=gemini`. |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | Default `30` / `3` — immediate in-adapter retries for transient failures only |
| `GEMINI_API_KEY` | Required when `LLM_PROVIDER=gemini` |
| `EXTRACTION_ENABLED` | Default `true` — kill switch; conversations work identically either way |
| `EXTRACTION_WINDOW_SIZE` | Default `6` messages of context per extraction |
| `EXTRACTION_MIN_MESSAGE_CHARS` | Default `25` — the heuristic gate's length threshold |
| `EXTRACTION_MAX_ATTEMPTS` | Default `3` — bounds total attempts including lease-reclaimed ones |
| `EXTRACTION_LEASE_SECONDS` | Default `600` — before the sweep reclaims a stale job |
| `EXTRACTION_SWEEP_BATCH_SIZE` / `EXTRACTION_SWEEP_LOOKBACK_DAYS` | Default `50` / `7` — bounds on the periodic sweep |
| `INTERNAL_API_TOKEN` | Guards `POST /internal/extraction/sweep` and (Phase 3) `POST /internal/embeddings/backfill`. Unset = both disabled (fail closed). |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` | Default `5` / `5` |

## Phase 3 additions

| Variable | Notes |
|---|---|
| `EMBEDDING_PROVIDER` | Default `fake` — the whole system, including semantic duplicate detection and vector search, runs with no API key. Set `gemini` for real embeddings. |
| `EMBEDDING_MODEL` | **Pin the exact model id here, never in code.** Required when `EMBEDDING_PROVIDER=gemini`. |
| `EMBEDDING_DIMENSION` | Default `768` — **must match the pgvector column's declared width** (migration `6202eeca7d8d`); `Database.connect()` asserts this at startup. Changing it needs a new migration. |
| `EMBEDDING_TIMEOUT_SECONDS` / `EMBEDDING_MAX_RETRIES` | Default `30` / `3` |
| `EMBEDDING_BACKFILL_BATCH_SIZE` | Default `50` — bounded embed budget per user, per backfill tick |
| `SEMANTIC_DUPLICATE_THRESHOLD` / `SEMANTIC_FLAG_THRESHOLD` | Default `0.90` / `0.80` — cosine similarity gates for `LIKELY_DUPLICATE` / `FLAGGED` |
| `SEMANTIC_ENTITY_FLAG_THRESHOLD` | Default `0.85` — cosine similarity gate for the entity-name ambiguity flag |
| `RETRIEVAL_WEIGHT_VECTOR` / `_GRAPH` / `_KEYWORD` / `_RECENCY` / `_TEMPORAL` / `_CONFIDENCE` | Default `1.0` / `1.0` / `0.5` / `0.2` / `0.5` / `0.3` — the hybrid ranking formula's weights, not normalized to sum to 1 |
| `RETRIEVAL_TOP_K` | Default `10` |

## Phase 4 additions

| Variable | Notes |
|---|---|
| `RETRIEVAL_GRAPH_HOPS` | Default `2` — multi-hop graph retrieval's hop budget, bounded `[1,3]` by the setting itself (the approved hard maximum) |
| `RETRIEVAL_GRAPH_DECAY_RATE` | Default `0.5` — exponential relevance decay, `decay_rate ** hops` |
| `RETRIEVAL_GRAPH_MAX_NODES` | Default `200` — bounds the nodes one traversal returns **for hybrid retrieval's graph channel and the agent's `traverse_graph` only** (`neighbourhood_with_paths`), and the anchor entities the graph channel expands per query. It does **not** bound `GET /entities/{id}/graph` — before Phase 8 that endpoint had no result bound at all; it is now bounded by `GRAPH_MAX_NODES` / `GRAPH_MAX_EDGES` |
| `INTERNAL_API_TOKEN` | Also now guards `POST /internal/review/reconcile` (same token as the extraction/embedding sweeps) |

## Phase 8 additions

| Variable | Notes |
|---|---|
| `GRAPH_MAX_NODES` / `GRAPH_MAX_EDGES` | Default `1000` / `5000` (hard max `10000` / `50000`) — result caps on `GET /entities/{id}/graph` (D1; values from the Stage 0 measurements, see `docs/PHASE8_PLAN.md` section 25) |
| `GRAPH_EXPLORE_MAX_NODES` | Default `150`, hard max `1000` — node budget per `/graph/*` slice; also the maximum length of `exclude_ids` |
| `GRAPH_EXPLORE_MAX_EDGES` | Default `400`, hard max `3000` — edge budget per slice |
| `GRAPH_EXPLORE_MAX_HOPS` | Default `2`, hard max `3` — neighborhood depth |
| `GRAPH_EXPLORE_MAX_MEMORIES_PER_ENTITY` | Default `5`, hard max `25` — memory nodes per entity with `include=memories` |

## Phase 9 additions

| Variable | Notes |
|---|---|
| `AUTH_MODE` | `session` (default) or `dev_header` (development/test only) |
| `AUTH_REGISTRATION_ENABLED` | Empty = on in development/test, **off** elsewhere |
| `AUTH_SESSION_TTL_HOURS` / `AUTH_SESSION_IDLE_HOURS` / `AUTH_SESSION_TOUCH_SECONDS` | `168` / `24` / `300` |
| `AUTH_COOKIE_NAME` / `AUTH_COOKIE_SECURE` / `AUTH_COOKIE_DOMAIN` | `sb_session` / empty = on outside development/test / host-only |
| `AUTH_SCRYPT_N` / `_R` / `_P` / `_MAXMEM` | `32768` / `8` / `3` / `67108864`; production floor N≥2^15, r≥8, N·r·p ≥ 2^15·8·3 |
| `TRUSTED_PROXY_IPS` | IPs/CIDRs whose `X-Forwarded-For` is trusted; empty = none |
| `CORS_VERCEL_PROJECT` | Required when `CORS_ALLOW_VERCEL_PREVIEWS=true` (now default `false`) |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_*` | See section 3a "Rate limits" |

`AUTH_SECRET` was removed from `.env.example`: server-side opaque sessions need
no signing secret.

`REDIS_URL` and `STORAGE_*` are present in `.env.example` for later phases and are
**unused through Phase 4**.
