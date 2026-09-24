# PHASE2_PLAN.md

# Phase 2 — Persistent Conversation Memory + LLM Extraction

**Status:** Awaiting approval. **No application code has been written.**

Phase 1 (graph memory foundation) is complete: 279 tests passing, CI green.
Phase 2 moves from manually structured graph memories to a system that learns
durable memories from conversation — without letting the model control the database.

## Governing principles

```text
LLM            proposes candidate information
Memory engine  decides whether and how it becomes persistent memory
Neo4j          stores the canonical state
```

```text
PostgreSQL  =  application / conversation state
Neo4j       =  canonical persistent AI memory   (never duplicated into PostgreSQL)
```

When evidence is insufficient, **under-extract**. A missed memory is an
inconvenience; an incorrect or silently overwritten memory undermines the product.

Decisions are recorded in
[ADR 0006](adr/0006-postgresql-for-application-state.md) (PostgreSQL boundary),
[ADR 0007](adr/0007-llm-proposes-application-decides.md) (LLM boundary and
conservative policy) and
[ADR 0008](adr/0008-extraction-trigger-strategy.md) (trigger strategy).

---

# 1. Updated repository structure

New and changed paths only; everything else is Phase 1 and untouched.

```text
backend/
├── alembic.ini                        NEW
├── migrations/                        NEW  Alembic
│   ├── env.py
│   └── versions/
├── app/
│   ├── core/
│   │   ├── config.py                  CHANGED  Postgres, LLM, extraction settings
│   │   └── errors.py                  CHANGED  + LLMUnavailableError,
│   │                                           LLMResponseInvalidError
│   ├── db/                            NEW      PostgreSQL layer
│   │   ├── engine.py                           async engine + session factory
│   │   ├── base.py                             declarative base
│   │   └── models.py                           SQLAlchemy tables
│   ├── graph/                          unchanged
│   ├── models/
│   │   ├── conversation.py            NEW      domain models
│   │   └── extraction.py              NEW      candidate schemas (LLM contract)
│   ├── llm/                           NEW
│   │   ├── base.py                             LLMClient protocol, LLMResult
│   │   ├── factory.py                          provider selection from config
│   │   ├── fake.py                             FakeLLMClient
│   │   ├── prompts/
│   │   │   └── extraction_v1.py                versioned prompt text
│   │   └── providers/
│   │       ├── gemini.py                       ONLY file importing google-genai
│   │       ├── openai.py                       (stub, Phase 2+)
│   │       └── anthropic.py                    (stub, Phase 2+)
│   ├── extraction/                    NEW      the pipeline
│   │   ├── gate.py                             deterministic heuristic gate
│   │   ├── window.py                           window construction + debounce
│   │   ├── extractor.py                        prompt -> validated candidates
│   │   ├── grounding.py                        span verification
│   │   ├── decisions.py                        NEW/DUPLICATE/.../IGNORE engine
│   │   ├── resolution.py                       entity resolution extension
│   │   ├── runner.py                           claim -> extract -> decide -> persist
│   │   └── sweep.py                            bounded reconciliation
│   ├── repositories/
│   │   ├── sql_base.py                NEW      UserScopedRepository (isolation)
│   │   ├── conversation_repository.py NEW
│   │   ├── message_repository.py      NEW
│   │   ├── extraction_repository.py   NEW
│   │   └── memory_repository.py        unchanged
│   ├── services/
│   │   ├── conversation_service.py    NEW
│   │   ├── extraction_service.py      NEW
│   │   ├── provenance_service.py      NEW
│   │   ├── memory_service.py           unchanged  (gains a second caller)
│   │   └── retrieval_service.py        unchanged
│   └── api/
│       ├── deps.py                    CHANGED  + db session, llm client, internal auth
│       └── routes/
│           ├── conversations.py       NEW
│           ├── extraction.py          NEW      status + internal sweep trigger
│           ├── provenance.py          NEW
│           ├── memories.py             unchanged
│           └── entities.py             unchanged
├── tests/
│   ├── unit/                          + gate, grounding, decisions, resolution,
│   │                                    fake LLM, idempotency, failure handling
│   ├── integration/                   + postgres/, extraction/  (need real services)
│   └── fixtures/llm/                  NEW      recorded extraction fixtures
└── eval/                              NEW
    ├── runner.py
    ├── metrics.py
    └── datasets/
        └── extraction_v1.jsonl                 10 labelled scenarios
```

`frontend/` is unchanged (Phase 3). `scripts/` gains `run_sweep.py` for local use.

---

# 2. PostgreSQL schema

Portable SQL only — no extensions, no vendor features — so the same migrations run
on local Homebrew PostgreSQL, Supabase and Neon with only `DATABASE_URL` changing.
**`pgvector` is deliberately not enabled** (Phase 3).

```sql
-- Identity reference only. Holds NO credentials in Phase 2; real authentication is
-- Phase 8. `id` is the same string used as the Neo4j namespace key, which is what
-- makes isolation comparable across the two stores.
CREATE TABLE users (
    id              TEXT PRIMARY KEY,
    external_auth_id TEXT UNIQUE,          -- populated in Phase 8
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
    id              UUID PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at TIMESTAMPTZ
);
CREATE INDEX conversations_user_recent
    ON conversations (user_id, last_message_at DESC);

CREATE TABLE messages (
    id              UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(id),
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    sequence        INTEGER NOT NULL,      -- monotonic within a conversation
    token_count     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- extraction bookkeeping
    extraction_status       TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_status IN
               ('pending','queued','in_progress','done','failed','skipped')),
    extraction_gate_reason  TEXT,          -- why the gate skipped it; makes the
                                           -- gate itself measurable
    UNIQUE (conversation_id, sequence)
);
CREATE INDEX messages_conversation_order ON messages (conversation_id, sequence);
CREATE INDEX messages_user              ON messages (user_id, created_at DESC);
CREATE INDEX messages_extraction_todo   ON messages (extraction_status)
    WHERE extraction_status IN ('pending','failed','skipped');

-- One unit of extraction work over a WINDOW of messages.
CREATE TABLE extraction_jobs (
    id                    UUID PRIMARY KEY,
    user_id               TEXT NOT NULL REFERENCES users(id),
    conversation_id       UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

    -- window: inclusive sequence range; target is the newest message in it
    target_message_id     UUID NOT NULL REFERENCES messages(id),
    window_start_sequence INTEGER NOT NULL,
    window_end_sequence   INTEGER NOT NULL,

    -- idempotency key component: provider:model:prompt_version:schema_version
    extractor_version     TEXT NOT NULL,

    status                TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_progress','done','failed','abandoned')),
    attempts              INTEGER NOT NULL DEFAULT 0,
    locked_at             TIMESTAMPTZ,     -- lease, for stale-worker reclaim
    locked_by             TEXT,
    next_attempt_at       TIMESTAMPTZ,
    error_code            TEXT,
    error_detail          TEXT,

    -- observability / cost (EVALUATION.md 13 and 21)
    provider              TEXT,
    model                 TEXT,
    prompt_version        TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    latency_ms            INTEGER,
    candidate_count       INTEGER,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ,

    -- THE idempotency constraint: the same work cannot be enqueued twice
    UNIQUE (target_message_id, extractor_version)
);
CREATE INDEX extraction_jobs_claimable ON extraction_jobs (status, next_attempt_at)
    WHERE status IN ('pending','failed');
CREATE INDEX extraction_jobs_lease     ON extraction_jobs (status, locked_at)
    WHERE status = 'in_progress';

-- What the LLM proposed, and what the engine decided. The audit trail that makes
-- extraction precision measurable and gives partial-failure retries something to
-- resume from.
CREATE TABLE extraction_candidates (
    id                 UUID PRIMARY KEY,
    extraction_job_id  UUID NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
    user_id            TEXT NOT NULL REFERENCES users(id),
    ordinal            INTEGER NOT NULL,
    candidate_hash     TEXT NOT NULL,      -- deterministic hash of the candidate
    payload            JSONB NOT NULL,     -- the validated candidate
    decision           TEXT NOT NULL
        CHECK (decision IN ('NEW','DUPLICATE','UPDATE','SUPERSEDING',
                            'CONFLICTING','INVALID','IGNORE')),
    decision_reason    TEXT NOT NULL,
    memory_id          TEXT,               -- opaque Neo4j id; NULL when nothing written
    applied_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- effect-level idempotency: a retry skips candidates already applied
    UNIQUE (extraction_job_id, candidate_hash)
);

-- Scenario G: flagged, never auto-merged.
CREATE TABLE entity_merge_candidates (
    id            UUID PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id),
    entity_id_a   TEXT NOT NULL,           -- Neo4j entity ids
    entity_id_b   TEXT NOT NULL,
    evidence      JSONB NOT NULL,
    score         REAL,
    status        TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (status IN ('unreviewed','merged','rejected')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, entity_id_a, entity_id_b)
);
```

`extraction_candidates` goes beyond the minimum three tables, and earns its place
three ways: it separates *what the model proposed* from *what became memory* (making
extraction precision/recall measurable per `EVALUATION.md` section 7), it is what a
crashed job resumes from, and it is what turns "why is this in my graph?" into a
real answer including the model and prompt version responsible.

### Neo4j additions — minimal

Phase 1's provenance model already covers message sources, so there is **no graph
schema change** for provenance:

* `Source { source_type: "message", reference: <message_id>, location: "<start>:<end>" }`
* `(:Memory)-[:CONFLICTS_WITH]-(:Memory)` — NEW, for the `CONFLICTING` decision
* `(:Entity)-[:POSSIBLE_DUPLICATE_OF]-(:Entity)` — NEW, evidence-carrying, never merged
* Provenance becomes **many-to-many in reads**: `DERIVED_FROM` is already a
  relationship, but `MemoryDetail.source` is singular (`head(collect(...))`). It
  becomes a list, because `DUPLICATE` attaches a *second* source to an existing
  memory — corroboration rather than a duplicate row.

---

# 3. Neo4j <-> PostgreSQL boundary

```text
┌──────────────────────────────┐        ┌────────────────────────────────┐
│ PostgreSQL                   │        │ Neo4j                          │
│ application / process state  │        │ canonical AI memory            │
├──────────────────────────────┤        ├────────────────────────────────┤
│ users                        │        │ (:Memory)                      │
│ conversations                │        │ (:Entity)                      │
│ messages            ◄────────┼────────┼── Source.reference             │
│ extraction_jobs              │        │ (:Source)                      │
│ extraction_candidates ───────┼────────┼──► memory_id (opaque pointer)  │
│ entity_merge_candidates ─────┼────────┼──► entity ids (opaque)         │
└──────────────────────────────┘        └────────────────────────────────┘
        raw input + process state              derived knowledge
```

**Rules, enforced by review and by tests:**

1. **One direction only.** PostgreSQL rows are read to *produce* memory. No Neo4j
   query reads PostgreSQL, and no PostgreSQL column stores memory content, status,
   validity windows, entities or relationships.
2. **No mirroring.** PostgreSQL is never a cache, projection or backup of the graph.
   `extraction_candidates.memory_id` and `entity_merge_candidates.entity_id_*` are
   opaque outbound pointers used for provenance display and idempotency — never
   copies of memory state.
3. **No cross-store transaction, and none needed.** The message commits first and is
   immutable, so a memory's provenance reference cannot dangle. Memory writes remain
   a single Neo4j transaction (Phase 1). A message whose extraction failed is
   *not yet extracted*, recorded in `extraction_jobs` — a recoverable state, not
   corruption.
4. **Dangling pointers are tolerated in one direction only.** If a Neo4j memory were
   ever deleted (nothing in the API can do this), `extraction_candidates.memory_id`
   would dangle harmlessly. The reverse is impossible by ordering.
5. **`user_id` is the shared isolation key** — a foreign-keyed column in PostgreSQL,
   a namespace property in Neo4j. Same string, enforced independently in both.

A test asserts rule 2 structurally: no SQLAlchemy model may declare a column whose
name collides with the memory-graph vocabulary (`content`, `predicate`, `valid_from`,
`valid_until`, `entity_type`, `memory_status`) outside `extraction_candidates.payload`.

---

# 4. Complete extraction sequence

```text
 1. POST /conversations/{id}/messages          (synchronous)
       │
 2. ├─► authorize: conversation.user_id == requesting user   -> 404 if not
 3. ├─► INSERT message (sequence = max+1)      committed before anything else
       │
 4. ├─► GATE (deterministic, no LLM)
       │     role == 'user'? length >= min? declarative/entity-like?
       │     not a bare question? not already extracted?
       │     │
       │     NO  -> extraction_status = 'skipped', gate_reason recorded ──► step 7
       │     YES
       │
 5. ├─► WINDOW (debounce)
       │     pending, unlocked job for this conversation?
       │       YES -> extend window_end_sequence, retarget   (one job, not two)
       │       NO  -> INSERT extraction_jobs
       │                ON CONFLICT (target_message_id, extractor_version) DO NOTHING
       │
 6. ├─► register BackgroundTask                (runs AFTER the response)
       │
 7. └─► 201 Created  ─────────────────────────► user is done waiting
                                                ════════════════════
       ┌─────────────────── asynchronous from here ───────────────────┐
       │
 8.    CLAIM  UPDATE ... SET status='in_progress', locked_at=now(),
       │             locked_by=$worker, attempts=attempts+1
       │       WHERE id IN (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT n)
       │       -> at-least-once delivery, safe under concurrency
       │
 9.    LOAD window messages (window_start_sequence..window_end_sequence)
       │
10.    LLMClient.complete_structured(prompt, schema=ExtractionResult)
       │       │ transient failure  -> status='failed', next_attempt_at = backoff
       │       │ invalid schema     -> ONE repair attempt, then 'failed'
       │       └─ record provider, model, prompt_version, tokens, latency
       │
11.    VALIDATE (Pydantic)            malformed -> whole result rejected
       │
12.    GROUND   every candidate's source_span must exist in its message, and
       │        claimed entity mentions must occur within it
       │        ungrounded -> decision = INVALID, recorded, not persisted
       │
       ══════════ the LLM's influence ends here ══════════
       │
13.    for each candidate, in order:
       │   a. already applied? (extraction_candidates.candidate_hash) -> skip
       │   b. persistence gate:  not persistent & no explicit-preference pattern
       │                         -> IGNORE
       │   c. resolve entities:  normalize + aliases (Phase 1)
       │                         near-miss -> POSSIBLE_DUPLICATE_OF, never merge
       │   d. canonicalize:      (normalized_source, predicate, normalized_target)
       │   e. look up existing edges for this user
       │   f. DECIDE (deterministic, see below)
       │   g. persist via Phase 1 MemoryService  (one Neo4j transaction)
       │   h. INSERT extraction_candidates row with decision + reason + memory_id
       │
14.    status='done', completed_at=now(); message.extraction_status='done'
       │
       └─► periodic SWEEP reconciles: failed-with-retries, expired leases,
           and gate-skipped messages (bounded, idempotent)
```

## The decision engine (step 13f)

Ordered and deterministic. **The LLM selects none of these branches**; it supplies
signals (`is_change`, `persistence`, `replaces_hint`) and code decides.

| # | Condition | Decision |
|---|---|---|
| 1 | Validation or grounding failed | `INVALID` |
| 2 | `persistence != persistent` and no explicit-preference pattern | `IGNORE` |
| 3 | No existing edge matches the triple | `NEW` |
| 4 | Exact triple, overlapping window, no change signal | `DUPLICATE` — attach Source to the existing memory, create nothing |
| 5 | Change signal, existing edge shares `(source, predicate)`, different target | `SUPERSEDING` — close the old window, create the new memory |
| 6 | Same triple, materially different temporal data | `UPDATE` — metadata/window only, never `content` |
| 7 | Existing **active** edge shares `(source, predicate)`, different target, **no** change signal | `CONFLICTING` — keep both, flag, never overwrite |
| 8 | Otherwise | `NEW` |

Row 7 is the load-bearing safety rule: it is what stops a model from silently
rewriting history when a user mentions two tools in one sentence.

## Scenario walkthrough

| Scenario | Input | Expected |
|---|---|---|
| **A** New fact | "I am building a project called Drawing Extractor." | `NEW` — project entity + memory |
| **B** Technology | "The project uses Gemini for classification." | `NEW` — `Drawing Extractor -uses-> Gemini` |
| **C** Replacement | "We switched from EasyOCR to PaddleOCR." | `SUPERSEDING` — EasyOCR edge closed + `superseded`; PaddleOCR active. Requires the **window** to resolve "we"/the project |
| **D** Temporary | "I'm tired today." | `IGNORE` — gate or persistence classification; no memory |
| **E** Preference | "I prefer Java for coding." | `NEW` — procedural/semantic preference memory |
| **F** Duplicate | "I use Gemini for classification." then "The project uses Gemini for classification." | `DUPLICATE` — second Source attached, no second memory |
| **G** Ambiguous | "Open AI" vs "OpenAI" | Two entities + `POSSIBLE_DUPLICATE_OF` with evidence. **No merge.** |

Scenario F is caught by exact-triple matching. **Semantic** duplicates ("classifies
with Gemini" vs "uses Gemini for classification") are **not** caught in Phase 2 —
that needs embeddings, i.e. Phase 3. Stated rather than implied.

---

# 5. LLM interface

Nothing outside `app/llm/providers/` may reference a provider. The rest of the
system depends only on `LLMClient`.

```python
# app/llm/base.py

class LLMClient(Protocol):
    async def complete_structured(
        self,
        *,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult[T]: ...

@dataclass(frozen=True)
class LLMResult[T]:
    value: T                   # already Pydantic-validated
    provider: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw: str                   # retained for failure analysis
```

* `value` is **already validated** — no caller ever sees provider JSON.
* Metadata travels with every result because `EVALUATION.md` section 21 requires
  reproducibility fields on every benchmark; capturing them at the boundary avoids a
  retrofit.
* Provider errors map into the existing taxonomy (`app/core/errors.py`), mirroring
  `graph/connection.py::_map_driver_error`:

| Provider condition | Application error | Retry? |
|---|---|---|
| timeout, 429, 5xx, connection reset | `LLMUnavailableError` (503-class) | yes, backoff |
| response not schema-valid | `LLMResponseInvalidError` | one repair attempt |
| auth failure, unknown model | `ConfigurationError` | no |

**Model identity lives only in configuration** (`LLM_PROVIDER`, `LLM_MODEL`). No
model string appears in application code, prompts or tests. The exact Gemini model
identifier and its current pricing are to be pinned from the provider's own
documentation at step 2.11 — not guessed at now.

## The extraction contract

```python
class Span(BaseModel):
    start: int; end: int

class CandidateEntity(BaseModel):
    name: str                                  # NAME, never an id
    entity_type: EntityType
    aliases: list[str] = []
    mention_span: Span | None = None

class CandidateRelationship(BaseModel):
    source: str; predicate: str; target: str   # NAMES, never ids
    confidence: Confidence = 1.0

class CandidateTemporal(BaseModel):
    valid_from_hint: str | None = None         # "last May", "2026-01" — free text
    valid_until_hint: str | None = None
    is_change: bool = False                    # an observation, not an instruction
    replaces_hint: str | None = None           # natural language, NOT a memory id

class CandidateMemory(BaseModel):
    statement: str                             # one atomic fact
    memory_type: MemoryType
    persistence: Literal["persistent", "temporary", "uncertain"]
    confidence: Confidence
    entities: list[CandidateEntity]
    relationships: list[CandidateRelationship]
    temporal: CandidateTemporal
    source_message_id: str                     # echoed; verified against the window
    source_span: Span                          # offsets into that message
    reasoning: str                             # short, for eval and debugging

class ExtractionResult(BaseModel):
    candidates: list[CandidateMemory]
```

Three properties are deliberate and non-negotiable (see
[ADR 0007](adr/0007-llm-proposes-application-decides.md)):

* **No identifiers.** The model cannot address an existing memory or entity.
* **No imperatives.** There is no field in which it can say supersede, delete or
  merge. `is_change` is an observation; the engine chooses the operation.
* **Grounding is mandatory.** `source_span` is verified against the actual message
  text, so a fabricated fact with no textual anchor is rejected deterministically
  rather than argued with.

---

# 6. FakeLLMClient design

```python
class FakeLLMClient:
    """Deterministic LLMClient. The default in every non-opt-in test."""

    def __init__(self, fixtures: dict[str, ExtractionResult] | None = None,
                 *, failure: Exception | None = None,
                 raw_override: str | None = None) -> None: ...
```

* **Fixture lookup by stable key** — a hash of the normalized prompt, or an explicit
  key — loaded from `tests/fixtures/llm/*.json`. Same input, same output, always.
* **Failure injection** — construct it to raise `LLMUnavailableError`,
  `LLMResponseInvalidError`, or a timeout, so retry and permanent-failure paths are
  tested without a network.
* **Malformed-output injection** — `raw_override` returns text that fails Pydantic
  validation, exercising the repair-then-fail path.
* **Call recording** — asserts the prompt included the expected window and that the
  gate suppressed the calls it should have.

Fixtures cover scenarios A–G plus adversarial cases: malformed JSON; schema-valid
but **ungrounded** spans (hallucination); a prompt-injection message; an empty
candidate list; a candidate naming an entity absent from the message; a candidate
with a `replaces_hint` that resolves to nothing.

**Tests must never require an API key, network access, or money.** This is what
keeps `ci.yml` free and deterministic, and it is a hard rule, not a preference.

---

# 7. Idempotency design

Designed for **at-least-once delivery with idempotent effects**. Exactly-once is not
assumed anywhere.

## Layer 1 — job identity

```text
extractor_version = "{provider}:{model}:{prompt_version}:{schema_version}"
UNIQUE (target_message_id, extractor_version)
```

Enqueue uses `ON CONFLICT DO NOTHING`, so the live path and the sweep cannot create
competing jobs for the same work. Changing the prompt or model *intentionally*
produces a new key, allowing deliberate re-extraction — which is also how
`EVALUATION.md` comparisons across prompt versions become possible.

## Layer 2 — effect identity

```text
candidate_hash = sha256(canonical_json(candidate))
UNIQUE (extraction_job_id, candidate_hash)
```

Before applying a candidate, the runner checks for an existing row with
`applied_at IS NOT NULL`. A job that wrote 2 of 3 memories and then crashed applies
only the third on retry.

## Layer 3 — the decision engine itself

A full re-run against an unchanged graph classifies every candidate as `DUPLICATE`
and writes nothing. **Idempotency falls out of the conservative policy** rather than
being bolted on — which also means Layers 1 and 2 are optimisations for cost and
audit clarity, not the only thing standing between a retry and duplicate memory.

## Concurrency

Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED` plus a lease (`locked_at`,
`locked_by`). Two workers never process one job simultaneously; a worker that dies
leaves an expired lease that the sweep reclaims.

---

# 8. Async job design

**No queue infrastructure.** `BackgroundTasks` plus a bounded sweep is sufficient for
Phase 2; Redis, Celery or arq would be speculative
(`ARCHITECTURE.md` section 17: "Not every operation needs a background job").

## Live path

`BackgroundTasks` registers the runner **after** the response is returned, so the
user waits for nothing. One job is claimed and processed per task.

## Debounce

A `pending`, unlocked job for the same conversation is **extended** rather than
duplicated: `window_end_sequence` advances and `target_message_id` is retargeted. A
burst of five messages produces one extraction over a five-message window instead of
five overlapping calls — a direct cost and duplicate-extraction reduction.

## Sweep — bounded and idempotent

Reconciles three distinct failure modes:

| Case | Query | Action |
|---|---|---|
| Retryable failure | `status='failed' AND attempts < max AND next_attempt_at <= now()` | re-claim |
| Dead worker | `status='in_progress' AND locked_at < now() - lease` | reclaim |
| **Gate mistake** | `messages.extraction_status='skipped'` within lookback | re-evaluate under the **window** gate |

The third case is why the sweep exists at all: *a heuristic mistake must not
permanently cost the user a memory.* A message that looked inert alone may be
meaningful once later messages supply context.

**Bounds** (all configurable): `EXTRACTION_SWEEP_BATCH_SIZE` rows per run,
`EXTRACTION_MAX_ATTEMPTS` per job, `EXTRACTION_SWEEP_LOOKBACK_DAYS` for skipped
re-evaluation, and one re-evaluation per message per `extractor_version`. It cannot
become an unbounded reprocessing storm, and it costs nothing when there is nothing to
do.

**Trigger:** a plain function, exposed through `POST /internal/extraction/sweep`
guarded by `INTERNAL_API_TOKEN`, so the managed host's own cron calls it — no new
infrastructure, and it stays observable. `scripts/run_sweep.py` runs it locally.

---

# 9. Failure and retry behaviour

**Extraction is an asynchronous enhancement, never a hard dependency for responding
to the user.**

```text
User sends message
        ↓
Message stored in PostgreSQL       ✓ committed
        ↓
Extraction job created             ✓ committed
        ↓
Response returned to user          ✓ succeeds regardless of what follows
        ↓
LLM unavailable
        ↓
Job -> failed, next_attempt_at set, error recorded
        ↓
Sweep retries (bounded)  ->  done, or eventually 'abandoned'
        ↓
Conversation continues working throughout
```

| Failure | Classification | Behaviour |
|---|---|---|
| LLM timeout | `LLMUnavailableError` | retry, exponential backoff |
| 429 / 5xx | `LLMUnavailableError` | retry, exponential backoff |
| Malformed response | `LLMResponseInvalidError` | **one** repair attempt, then `failed` |
| Schema-valid but ungrounded | per-candidate `INVALID` | recorded; job still succeeds |
| Auth failure / unknown model | `ConfigurationError` | **no retry**; logged at ERROR |
| Neo4j unavailable mid-apply | `GraphUnavailableError` | transaction rolls back; job retried |
| Worker dies mid-job | expired lease | sweep reclaims; Layer 2 resumes |
| Attempts exhausted | — | `status='abandoned'`, visible via the status endpoint |

**Nothing is silently swallowed.** Every failure writes `error_code` and
`error_detail` to `extraction_jobs`, logs at ERROR with the request id, and surfaces
through `GET /extraction/status`. Retrying a malformed response unchanged would just
burn money, which is why that path repairs once and then stops.

`/health/ready` is **not** affected by LLM health: extraction is not a request
dependency, and a provider outage must not take the service out of rotation.
`EXTRACTION_ENABLED=false` is a kill switch that stops extraction while leaving
conversations fully functional.

---

# 10. Security and user-isolation model

Isolation now spans two stores. The same `user_id` string is the key in both, and
each is enforced independently — neither inherits the other's guarantees.

## PostgreSQL

* Every table carries `user_id` with a foreign key to `users`.
* **All queries go through `UserScopedRepository`**, whose `_select(Model)` helper
  applies `.where(Model.user_id == self._user_id)` automatically. Analogous to the
  Phase 1 Cypher chokepoint.
* **Static audit:** an AST-based test asserts no module under `app/repositories/`
  constructs a bare `select(`/`update(`/`delete(` outside that helper. This is the
  SQL counterpart to `test_cypher_safety.py`. Honestly, it is a weaker check than the
  Cypher one — SQLAlchemy builds queries programmatically rather than as inspectable
  strings — so it is backed by per-method isolation tests rather than relied on alone.
* Nested ownership is verified, not assumed: posting to a conversation checks
  `conversation.user_id`, and a mismatch returns **404, not 403**.
* PostgreSQL row-level security is noted as Phase 8 defence-in-depth (Supabase
  supports it); it is not a Phase 2 substitute for application checks.

## Neo4j

Unchanged from Phase 1: repository chokepoint, `$user_id` on every statement, the
static audit, and path-scoped filtering on traversals.

## Cross-store

* `user_id` is carried explicitly through the pipeline and **never** taken from LLM
  output.
* A job's `user_id` is verified against its message's `user_id` before extraction.
* Writes to Neo4j use the job's `user_id`, so a model that hallucinated a different
  user cannot affect anything.
* Extraction can only ever write within the originating message's namespace.

## Prompt injection

Structurally contained by [ADR 0007](adr/0007-llm-proposes-application-decides.md):
the model can only propose, cannot address ids, cannot issue operations, and cannot
leave the user's namespace. Extracted content is never fed back into a prompt as
instruction. In Phase 2 the worst case is a user polluting their own memory; document
ingestion (Phase 6) makes this a genuine cross-trust boundary, and this is the
boundary that makes that tractable.

## Still absent

**Authentication remains Phase 8.** `X-User-Id` is still a development placeholder,
and startup still fails when `APP_ENV=production` with
`ALLOW_USER_ID_HEADER=true`. Phase 2 adds `INTERNAL_API_TOKEN` for the sweep
endpoint, which is *not* user-scoped and must not be exposed publicly.

---

# 11. New environment variables

Added to `.env.example` with annotations. `DATABASE_URL` already exists but is
unused until now.

```env
# --- PostgreSQL (now required) ---------------------------------------------
# Local:      postgresql+asyncpg://localhost/ai_second_brain
# Supabase/Neon: postgresql+asyncpg://user:pw@host/db?ssl=require
DATABASE_URL=
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=5

# --- LLM -------------------------------------------------------------------
LLM_PROVIDER=gemini            # gemini | openai | anthropic | fake
LLM_MODEL=                     # PIN the exact model id here; never in code
GEMINI_API_KEY=
LLM_TIMEOUT_SECONDS=30
LLM_MAX_RETRIES=3

# --- Extraction ------------------------------------------------------------
EXTRACTION_ENABLED=true        # kill switch; conversations work either way
EXTRACTION_PROMPT_VERSION=v1
EXTRACTION_SCHEMA_VERSION=v1
EXTRACTION_WINDOW_SIZE=6       # messages of context per extraction
EXTRACTION_MIN_MESSAGE_CHARS=25
EXTRACTION_MAX_ATTEMPTS=3
EXTRACTION_LEASE_SECONDS=600   # before the sweep reclaims a stale job
EXTRACTION_SWEEP_BATCH_SIZE=50
EXTRACTION_SWEEP_LOOKBACK_DAYS=7
EXTRACTION_RECHECK_SKIPPED=true
EXTRACTION_DAILY_TOKEN_BUDGET=  # optional hard stop; empty = unlimited

# --- Internal operations ---------------------------------------------------
INTERNAL_API_TOKEN=            # guards POST /internal/extraction/sweep
```

`REDIS_URL`, `STORAGE_*` and `EMBEDDING_*` remain present for later phases and stay
**unused** in Phase 2.

---

# 12. New dependencies

| Package | Why |
|---|---|
| `sqlalchemy[asyncio]` | PostgreSQL core/ORM, async to match FastAPI |
| `asyncpg` | PostgreSQL driver |
| `alembic` | Migrations (`ARCHITECTURE.md` section 27) |
| `google-genai` | Gemini adapter — imported **only** in `app/llm/providers/gemini.py` |
| *(dev)* `pytest-asyncio` | already present |

`greenlet` arrives transitively with SQLAlchemy's async support.

**Deliberately NOT added:**

* **LangChain / LangGraph.** The whole value of this phase is the explicit boundary
  between "LLM proposes" and "code decides"; a chain framework's purpose is to blur
  exactly that boundary, and it would add a large dependency surface plus its own
  abstractions over what is fundamentally one structured call. `complete_structured()`
  is the entire requirement.
* **Redis / Celery / arq.** `BackgroundTasks` plus the sweep suffices. Revisit on a
  measured problem.
* **`instructor` and output-parsing libraries.** Provider-native structured output
  plus Pydantic validation is strictly better, since we validate regardless.
* **`pgvector`.** Phase 3.
* **`freezegun`.** Clocks are already injected (`now_fn`), which is better.

---

# 13. Local development without Docker

Both services run natively. (On the current development machine PostgreSQL 17 is
already running via Homebrew, and Neo4j was installed the same way during Phase 1.)

```bash
# PostgreSQL
brew install postgresql@17 && brew services start postgresql@17
createdb ai_second_brain

# Neo4j (from Phase 1)
brew services start neo4j

# Configure
cp .env.example .env
#   DATABASE_URL=postgresql+asyncpg://localhost/ai_second_brain
#   NEO4J_URI=bolt://localhost:7687   (+ username/password)
#   LLM_PROVIDER=fake                 <- no API key needed for development

cd backend
uv sync --extra dev
uv run alembic upgrade head                      # PostgreSQL schema
uv run python ../scripts/init_graph.py           # Neo4j schema
uv run uvicorn app.main:app --reload --port 8000

# Tests — no API key, no network, no cost
uv run pytest tests/unit -q                      # 224 Phase 1 + Phase 2 additions
uv run pytest -q                                 # integration skips without services

# Sweep, on demand
uv run python ../scripts/run_sweep.py
```

`LLM_PROVIDER=fake` is a first-class configuration, so the whole system is runnable
end to end with no provider account at all.

---

# 14. Deployment — Vercel + managed backend

```text
                    ┌──────────────┐
                    │    Vercel    │   Next.js (Phase 3)
                    └──────┬───────┘
                           │ HTTPS   NEXT_PUBLIC_API_URL
                           ▼
                    ┌──────────────┐
                    │   FastAPI    │   Railway / Render
                    │ Managed Host │   + platform cron -> /internal/extraction/sweep
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         PostgreSQL     Neo4j Aura    LLM API
         Supabase/Neon  AI memory     Gemini
         conversations
```

**No Docker. No Kubernetes.** Unchanged from Phase 1.

| Concern | Approach |
|---|---|
| Migrations | **Pre-deploy release step** (`alembic upgrade head`), never at startup — concurrent instances must not race |
| Secrets | Host secret store: `DATABASE_URL`, `NEO4J_*`, `GEMINI_API_KEY`, `INTERNAL_API_TOKEN` |
| Sweep trigger | Platform cron (Railway/Render both provide it) calling the internal endpoint |
| Readiness | `/health/ready` gains a PostgreSQL check; **LLM health excluded** on purpose |
| Connection pools | Small (`DATABASE_POOL_SIZE=5`); Supabase/Neon pool externally too |
| Frontend | Unaffected; the `NEXT_PUBLIC_API_URL` contract is unchanged |

**One real constraint, worth deciding now:** in-process background extraction needs a
host that keeps executing after the response is sent. Fine on Railway and Render.
On **Cloud Run** it requires CPU-always-allocated or `min-instances >= 1`, or
extraction is throttled mid-flight. This is the first Phase 2 decision that makes
host choice non-arbitrary — it argues for Railway or Render.

---

# 15. Implementation order

Steps 2.1–2.10 require **no API key and no network**. The real provider appears only
at 2.11, after the deterministic core is fully tested.

| Step | Deliverable | Exit criteria |
|---|---|---|
| **2.0** | ADRs 0006–0008 + evaluation dataset + LLM fixtures. **No code.** | *This document approved*; 10 scenarios labelled |
| **2.1** | PostgreSQL foundation: engine, session, Alembic, `users`/`conversations`/`messages`, readiness check | `alembic upgrade`/`downgrade` clean; integration tests green |
| **2.2** | Conversation API (**no LLM**): create/list conversations, post/list messages, ordering, ownership | CRUD + isolation + ordering tests green; **Phase 1's 279 still green** |
| **2.3** | `UserScopedRepository` + SQL isolation audit | Cross-user access returns 404 in every new endpoint; AST audit passes |
| **2.4** | `LLMClient` protocol, `LLMResult`, error mapping, `FakeLLMClient` + fixtures | Fake-only golden tests; **no provider code yet** |
| **2.5** | Extraction schemas + Pydantic validation + **span grounding** | Rejects malformed, ungrounded, injected and entity-absent fixtures |
| **2.6** | Deterministic gate + window construction + debounce | Scenario D suppressed; gate reasons recorded; burst produces one job |
| **2.7** | Entity resolution extension: aliases, ambiguity detection, `POSSIBLE_DUPLICATE_OF`, merge candidates | Scenario G flags without merging; **wrong-merge count zero** |
| **2.8** | **Memory decision engine** — all 7 classifications | Full confusion-matrix coverage over A–G. Largest test surface in the phase |
| **2.9** | Wire extraction into Phase 1 `MemoryService`; provenance many-to-many | Scenarios A/B/C/E/F produce correct graph state; Phase 1 code unmodified |
| **2.10** | Async execution: `extraction_jobs`, claim/lease, all three idempotency layers, retries, sweep, `scripts/run_sweep.py` | Same extraction twice -> **no duplicate memory**; killed mid-job resumes; skipped messages re-evaluated |
| **2.11** | `GET /memories/{id}/provenance` + `GET /extraction/status` | Memory -> Source -> Message -> Conversation resolves; failures visible |
| **2.12** | Gemini adapter + opt-in `-m llm` workflow | Exact model id and pricing pinned in config; schema-valid output confirmed |
| **2.13** | Evaluation harness + **measured baseline** | Real numbers published; naive "store every message" baseline compared; **no illustrative values** |

## Testing requirements carried through every step

* **Phase 1's 279 tests must stay green.** Any change to them is a regression to
  justify, not a convenience.
* Default CI stays free and deterministic: no API key, no network, no cost.
* New integration tests are split by dependency — `tests/integration/postgres/` and
  the existing Neo4j suite — each skipping cleanly when its service is absent, and
  each in the opt-in workflow.
* Mandatory Phase 2 cases: conversation/message creation, ordering, timestamps, user
  isolation, job persistence; valid and malformed LLM output; missing fields;
  hallucinated entities; temporary vs persistent statements; duplicates; conflicts;
  temporal updates; **run the same extraction twice and assert no duplicate memory**;
  LLM timeout, provider error, retry, permanent failure — with conversation
  persistence succeeding throughout.

## Evaluation dataset (`backend/eval/datasets/extraction_v1.jsonl`)

Ten labelled scenarios, each with an expected memory state:

| # | Case | Expected |
|---|---|---|
| 1 | New project | `NEW` project entity + memory |
| 2 | New technology | `NEW` `uses` relationship |
| 3 | User preference | `NEW` preference memory |
| 4 | Temporary statement | `IGNORE`, nothing persisted |
| 5 | Technology replacement | `SUPERSEDING`; old edge closed, not deleted |
| 6 | Duplicate statement | `DUPLICATE`; second Source, no second memory |
| 7 | Ambiguous entity | two entities + `POSSIBLE_DUPLICATE_OF`; no merge |
| 8 | Contradictory statement | `CONFLICTING`; both retained and flagged |
| 9 | Historical query | as-of query returns the fact true at that time |
| 10 | Multi-turn accumulation | facts across turns resolve to one entity |

Metrics: extraction precision/recall/F1 (triple-level); persistence-classification
accuracy; **wrong-merge count reported separately with a target of zero**, never
averaged against correct merges; decision confusion matrix; temporal accuracy;
provenance accuracy; cost and latency per conversation from `extraction_jobs`.

Two harnesses, deliberately separate: a **deterministic** one (fixtures, default CI)
measuring the decision engine and resolution — the part that must be correct — and a
**model-quality** one (real provider, opt-in, costs money) measuring extraction.
Conflating them would make a quality regression indistinguishable from prompt drift.

Baseline to beat: "store every user message as a memory". If gated extraction does
not beat that on precision, the gate is not earning its complexity — and per
`EVALUATION.md` section 22, that negative result is worth knowing.
