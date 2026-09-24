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

**Phase 1 has no authentication.** The user id comes from the `X-User-Id` header,
falling back to `DEV_USER_ID`. This is a placeholder, not a security boundary — see
[ADR 0005](adr/0005-phase1-identity-placeholder.md). Startup fails if
`APP_ENV=production` while `ALLOW_USER_ID_HEADER=true`.

---

# 4. Endpoints

All requests take an optional `X-User-Id` header. Every response carries
`X-Request-ID`. Live OpenAPI schema at `/docs`.

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
| `GET` | `/entities/search?q=` | Substring search over names and aliases |
| `GET` | `/entities/by-name?name=` | Exact resolution by normalized name |
| `GET` | `/entities/{id}` | Entity with memory and relationship counts |
| `GET` | `/entities/{id}/relationships` | Incident edges (`direction`, `as_of`) |
| `GET` | `/entities/{id}/memories` | Memories mentioning the entity |
| `GET` | `/entities/{id}/history` | **Current vs historical state** (`as_of`) |
| `GET` | `/entities/{id}/graph?hops=N` | N-hop subgraph, bounded by `GRAPH_MAX_HOPS` |

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

All under the same `X-User-Id` header as Phase 1, except the internal router.

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

Every endpoint below is user-scoped through `X-User-Id` like the rest of the
API. Write endpoints require `if_version` (optimistic concurrency): a stale
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
| `DEV_USER_ID` | Phase 1 fallback identity |
| `ALLOW_USER_ID_HEADER` | Must be `false` when `APP_ENV=production` |
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
| `RETRIEVAL_GRAPH_MAX_NODES` | Default `200` — bounds a single neighbourhood traversal's result size |
| `INTERNAL_API_TOKEN` | Also now guards `POST /internal/review/reconcile` (same token as the extraction/embedding sweeps) |

`REDIS_URL` and `STORAGE_*` are present in `.env.example` for later phases and are
**unused through Phase 4**.
