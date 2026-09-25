# ARCHITECTURE.md

# AI Second Brain

## Technical Architecture

---

# 1. Architecture Philosophy

The system should be modular but not unnecessarily distributed.

Primary principles:

```text
Clear boundaries
Simple infrastructure
Observable AI behavior
Provider independence
Testability
Scalability where justified
```

Do not introduce microservices without a demonstrated reason.

---

# 2. System Components

Major logical components:

```text
Frontend
Backend API
Agent
Memory Engine
Retrieval Engine
Graph Store
Vector Store
Relational Database
Document Ingestion
Background Workers
Evaluation Engine
Authentication
Observability
```

---

# 3. High-Level Architecture

```text
                         USER
                           |
                           v
                  ┌─────────────────┐
                  │    Next.js      │
                  │    Frontend     │
                  └────────┬────────┘
                           |
                           v
                  ┌─────────────────┐
                  │    FastAPI      │
                  │      API        │
                  └────────┬────────┘
                           |
              ┌────────────┼────────────┐
              |            |            |
              v            v            v
           Agent        Memory       Documents
              |          Engine        Engine
              |            |            |
              |            v            |
              |       Retrieval         |
              |          Engine          |
              |            |             |
              |     ┌──────┴──────┐      |
              |     |             |      |
              |     v             v      |
              |   Vector        Graph    |
              |   Search        Search   |
              |     |             |      |
              |     └──────┬──────┘      |
              |            v             |
              |         Reranker         |
              |            |             |
              └────────────┼─────────────┘
                           |
                           v
                         LLM
```

---

# 4. Frontend

Technology:

* Next.js
* TypeScript
* Tailwind
* shadcn/ui

Main routes:

```text
/
 /chat
 /graph
 /memories
 /entities/:id
 /documents
 /settings
 /login
```

---

# 5. Backend

FastAPI should expose logical APIs.

Potential routes:

```text
POST /api/chat
GET  /api/conversations
GET  /api/memories
GET  /api/memories/:id
GET  /api/entities/:id
GET  /api/graph
POST /api/documents
GET  /api/documents
POST /api/search
```

Do not finalize API contracts until implementation begins.

---

# 6. Memory Engine

Responsibilities:

* identify candidate memories
* extract entities
* extract relationships
* resolve entities
* detect duplicates
* detect conflicts
* determine temporal state
* persist/update memories
* associate provenance

Conceptual interface:

```text
MemoryEngine
 ├── extract()
 ├── resolve()
 ├── validate()
 ├── store()
 ├── update()
 ├── invalidate()
 └── history()
```

**Extended in Phase 4** (ADR 0010, PHASE4_PLAN.md section 6): duplicate/flag/
conflict detection now has a review workflow, not just a decision recorded in
extraction bookkeeping. `LIKELY_DUPLICATE`/`FLAGGED`/`CONFLICTING` candidates
are written to a `memory_review_candidates` PostgreSQL row (workflow state,
never a copy of canonical memory) by `app.extraction.runner`; a reviewer
resolves them through `app/services/review_action_service.py`. A confirmed
duplicate is never deleted: it is marked `MemoryStatus.MERGED` (a new,
deliberately temporal-orthogonal status — distinct from `SUPERSEDED`, which
means "this fact changed over time", not "this is a redundant assertion") and
gains a `DUPLICATE_OF` edge to the memory kept canonical. A resolved conflict
either supersedes one side (reusing the existing supersession machinery), is
acknowledged with both sides left untouched (`keep_both`), or is rejected
(the flag was wrong, the memory returns to `ACTIVE`) — the `CONFLICTS_WITH`
edge itself is never removed, since a conflict having been flagged is itself
part of the history the system exists to keep. Every review action is
recorded in an append-only `review_audit_log`, distinguishing "LLM proposed"
(`ExtractionCandidate.payload`/`decision`, unchanged) from "human corrected"
(this table) without ever overwriting the original decision. Writes are
optimistic-concurrency-guarded (`if_version`) and idempotent (a repeated
action returns the existing state, never a second mutation) — see
`app.services.review_action_service`'s module docstring for the exact
Neo4j-then-PostgreSQL-then-audit ordering and its one documented, narrow,
reconciliation-repairable crash window.

---

# 7. Graph Engine

Responsibilities:

* create entities
* create relationships
* query graph
* traverse graph
* update relationships
* preserve relationship history

Neo4j is the preferred implementation.

**Extended in Phase 4** (ADR 0010, PHASE4_PLAN.md section 9): entity
consolidation is via redirect, never deletion or rewrite. Approving a merge
(`POST /review/entities/{id}/approve-merge`) copies the non-canonical
entity's relationships onto the canonical one and creates a
`(merged)-[:MERGED_INTO]->(canonical)` edge; the merged entity node itself is
untouched. Every entity-retrieval path — `RetrievalService.get_entity()`,
`find_entity_by_name()`, and hybrid retrieval's graph-channel entity-mention
scan — resolves through ONE canonical mechanism, `EntityRepository.resolve()`
(exposed at the service layer as `RetrievalService.resolve_entity()`, a thin
pass-through, never a second implementation), rather than scattered
`if entity.merged_into` checks — it follows redirect chains, detects and
safely terminates on cycles (Cypher's own per-path relationship-uniqueness
makes this free, no manual visited-set needed), and is bounded to
`_MAX_REDIRECT_DEPTH` hops. A merge is reversible
(`POST /review/entities/{id}/reverse-merge`): the copied relationships
(tagged with the merge's id) and the redirect are removed, restoring both
entities to their pre-merge state (aliases excepted — a documented,
accepted limitation).

**Extended in Phase 8** (ADR 0018, `docs/PHASE8_PLAN.md`, API reference in
`docs/API.md` section 4c): the graph is exposed to a future Memory Explorer
only as a **bounded, read-only projection**. `GraphExplorationService`
(`app/services/graph_exploration_service.py`) cuts `GraphSlice`s from the
canonical graph through `app/repositories/graph_repository.py`. That
repository traverses breadth-first, one bounded query per hop, rather than
with a variable-length pattern. It applies the temporal/status scope inside
the Cypher on every hop, compares validity windows by instant (UTC-normalised
on both sides), and fetches budget + 1 rows so truncation flags are exact.
Merge resolution reuses `RetrievalService.resolve_entity()`. Provenance
reuses `ProvenanceService`, now batched through `resolve_sources()`. Memory
history reuses the walk extracted from the agent's `get_memory_history` tool
(`app/services/memory_history.py`). Storage is never changed to suit
presentation: no new labels, relationship types, indexes or tables. The
pre-existing `GET /entities/{id}/graph` is now result-capped
(`GRAPH_MAX_NODES` / `GRAPH_MAX_EDGES`, `truncated`), a change made only
after Stage 0 measured it returning ~89 MB from one request at 10k
synthetic entities.

---

# 8. Vector Engine

Responsibilities:

* generate embeddings
* store embeddings
* perform similarity search
* retrieve semantic memories

PostgreSQL + pgvector is the initial implementation.

**Implemented in Phase 3** (ADR 0009, `docs/PHASE3_PLAN.md`): `app/embeddings/`
(`EmbeddingClient` abstraction, `FakeEmbeddingClient`, `GeminiEmbeddingClient`),
`app/repositories/embedding_repository.py`, and the `memory_embeddings` table
(pgvector, HNSW/cosine). Embeddings are a disposable, regenerable index of the
canonical Neo4j `Memory` content — never a second source of truth for it.

---

# 9. Retrieval Engine

Retrieval should abstract the underlying sources.

Conceptually:

```text
Retriever
 ├── VectorRetriever
 ├── GraphRetriever
 ├── KeywordRetriever
 └── HybridRetriever
```

Hybrid retrieval should combine results.

**Implemented in Phase 3**: `app/services/hybrid_retrieval_service.py`. The
vector, graph (entity-anchored) and keyword (Phase 1's CONTAINS search, kept
unchanged) channels are merged by `memory_id`, filtered by the query's detected
temporal intent against Neo4j's authoritative state, then ranked. `POST /search`
exposes it.

**Extended in Phase 4** (PHASE4_PLAN.md sections 1-4): the graph channel is now
bounded multi-hop, not just direct mention. A query-mentioned entity's
neighbourhood is traversed out to `RETRIEVAL_GRAPH_HOPS` (default 2, hard max 3
via the setting's own bound), via `EntityRepository.neighbourhood_with_paths` —
the same traversal the interactive `GET /entities/{id}/neighbourhood` endpoint
uses, extended to also capture the real shortest path per node (never an
LLM-generated explanation). Each discovered memory's graph relevance is
`RETRIEVAL_GRAPH_DECAY_RATE ** hops` (exponential, hop 0 = 1.0); when the same
memory is reachable via more than one anchor or path, only the shortest
(highest-scoring) path is kept ("merge, not concatenate", applied once across
entities and once across memories — see `HybridRetrievalService._graph_candidates`
and `GraphHit`'s docstring). `RetrievalExplanation` reports the actual
`graph_distance`/`graph_path` behind every graph-channel result.

Explosion protection is the hop bound plus `RETRIEVAL_GRAPH_MAX_NODES` (Cypher's
own `LIMIT`) — no additional fan-out cap. A dense-graph benchmark
(`eval/graph_scale_benchmark.py`, deterministic synthetic graphs at 100/1,000/
10,000 nodes with artificially high-degree "hub" entities) measured the actual
scaling bottleneck as a missing Neo4j index on `RELATES_TO.memory_id` (every
`MemoryService.get()` call scans a user's whole relationship set to find one
memory's own edges) — not the multi-hop traversal itself, and not too many
candidates. Adding that index (`app/graph/schema.py`) brought hybrid search
latency at 10,000 nodes from 39-113s down to ~0.5-1.6s; no fan-out cap was
added, since the benchmark did not demonstrate a need for one once the real
cause was fixed. These are relative, single-machine measurements, not a
production-scale claim.

---

# 10. Reranking

After retrieval:

```text
Candidates
 ↓
Scoring
 ↓
Reranking
 ↓
Top Context
```

Potential signals:

* semantic similarity
* graph relevance
* recency
* temporal validity
* confidence
* relationship relevance
* source quality

**Implemented in Phase 3**: `app/semantic/ranking.py` — an explicit, configurable
linear weighted sum over vector/graph/keyword/recency/temporal/confidence
signals (`RETRIEVAL_WEIGHT_*`), deliberately not a learned reranker; see ADR 0009
and `docs/PHASE3_PLAN.md` section 8.1 for the rationale. Source quality is not
yet a distinct signal.

The exact scoring algorithm should be evaluated rather than assumed.

---

# 11. Agent Engine

Implemented in Phase 5 (docs/PHASE5_PLAN.md) as a small custom loop, not
LangGraph or any other agent framework:

```text
POST /conversations/{id}/agent-turn
 |
 v
AgentService.run_turn
 |
 +-- build_agent_prompt (system prompt + conversation window + tool results so far)
 |
 +-- LLMClient.complete_structured(schema=AgentStep)   <- the SAME primitive
 |        |                                               extraction uses; no
 |        v                                               second LLM abstraction
 |   ToolCallStep | FinalAnswerStep (discriminated union)
 |
 +-- dispatch_tool (app/agent/tools.py) -- validates args, enforces per-tool
 |    timeout, maps every failure to a structured ToolResult
 |
 +-- loop again (bounded by AGENT_MAX_TOOL_CALLS, loop-detected on an
 |    identical repeated tool+args, bounded by AGENT_TURN_TIMEOUT_SECONDS)
 |
 v
FinalAnswerStep, with cited_memory_ids structurally validated against every
memory id any tool call actually returned this turn (never trusted at face
value -- one repair attempt, then citations are stripped rather than
accepted if still invalid)
```

`AgentService` never accesses Neo4j or PostgreSQL directly, and never
constructs its own service -- it is handed a `ToolDependencies` bundle
(existing `HybridRetrievalService`/`RetrievalService`/`MemoryService`/
`ProvenanceService` instances) by the API layer's dependency injection, the
same way every other route gets its services.

---

# 12. Memory Tools

Implemented (`app/agent/tools.py`), all wrapping existing Phase 1-4 services
-- none reimplements retrieval, resolution, or decision logic:

```text
search_memory           hybrid retrieval (vector + graph + keyword)
get_entity               name-or-id lookup, resolves merge redirects
traverse_graph           bounded multi-hop neighbourhood walk
get_entity_state         current vs. historical relationships, as-of a date
get_memory_history       supersedes/duplicate/conflict chain for one memory
get_source               provenance: source type, reference, source message
propose_memory_update    the only write; feature-gated by AGENT_WRITE_ENABLED
                         (default false); see ADR 0011
```

Every tool:

* takes `user_id` as a Python parameter supplied by `AgentService` from the
  authenticated request -- **no tool's argument schema has a `user_id`
  field**, so there is no code path by which a model's output could reach a
  service call as `user_id` (checked structurally by
  `tests/unit/test_agent_models.py`).
* validates its raw arguments against a strict (`extra="forbid"`) Pydantic
  schema before doing anything else (`app/models/agent.py`).
* returns a bounded, compact `ToolResult` -- content, entities, temporal
  state, confidence/status, provenance reference -- never a raw
  `MemoryDetail`/`EntityDetail`/domain object (PHASE5_PLAN.md section 10).
* maps every failure (not-found, graph/vector unavailable, timeout) to a
  structured status the model can react to; a tool call never raises out of
  `dispatch_tool`.

`propose_memory_update` is the one exception to "read-only, wraps a
service": it constructs a `CandidateMemory` from the model's proposal and
routes it through `ExtractionRunner._apply_candidate` -- the exact pipeline
passive extraction uses (ADR 0011) -- under a real, audited
`extraction_jobs`/`extraction_candidates` row. It never writes to Neo4j
directly, and `source_message_id`/`source_span` are always the turn's real,
already-persisted user message, never taken from the model.

---

# 13. Temporal Model

Memory and relationships should support temporal metadata.

Conceptual fields:

```text
created_at
updated_at
valid_from
valid_until
status
```

Example:

```text
Project
 |
 ├── uses → EasyOCR
 |          valid_until = 2026-05-01
 |
 └── uses → PaddleOCR
            valid_from = 2026-05-01
```

Exact date semantics should be carefully defined during implementation.

---

# 14. Provenance Model

Every extracted memory should be traceable.

Potential structure:

```text
Memory
 |
 └── Source
       ├── type
       ├── document_id
       ├── conversation_id
       ├── message_id
       └── location
```

The exact schema may evolve.

---

# 15. Data Isolation

All user-owned records must have:

```text
user_id
```

Backend authorization must ensure:

```text
requested_user == authenticated_user
```

unless a future shared-memory feature explicitly permits otherwise.

**Phase 8** (ADR 0019): the static Cypher isolation audit
(`backend/tests/unit/test_cypher_safety.py`) no longer depends on a
hand-maintained module list. A meta-test walks `app/repositories/`, finds
every module holding Cypher-shaped constants, and fails if any is not
audited, so a new repository module cannot silently escape the
`$user_id` rule.

**Phase 9** (ADRs 0020–0024): `authenticated_user` is now real. The one
identity dependency (`app.api.deps.get_auth_context` →
`get_current_user_id` → `CurrentUser`) resolves a verified server-side
session (PostgreSQL, token stored only as SHA-256) to the account's
`users.id`, the unchanged memory-ownership key. So
`requested_user == authenticated_user` holds by construction: no route,
service, tool or LLM output can supply a different id. Structural tests
(`backend/tests/unit/test_auth_audit.py`) enumerate every route (each must be
public, internal-token or authenticated) and every request, tool-argument and
LLM-output schema (none may carry an ownership field). The development
`X-User-Id` placeholder survives only as `AUTH_MODE=dev_header`, which cannot
start in a production-like environment.

---

# 16. Document Processing

Document processing should eventually run asynchronously.

```text
Upload
 ↓
Object Storage
 ↓
Job Queue
 ↓
Worker
 ↓
Parser
 ↓
Chunking
 ↓
Extraction
 ↓
Entity Resolution
 ↓
Graph Update
 ↓
Embedding
 ↓
Vector Update
```

---

# 17. Background Jobs

Possible jobs:

```text
document_ingestion
embedding_generation
memory_consolidation
graph_update
evaluation_run
```

Not every operation needs a background job.

Use asynchronous processing when latency would otherwise make the user experience poor.

**Implemented**: the extraction sweep (`POST /internal/extraction/sweep`,
Phase 2), the embedding backfill (`POST /internal/embeddings/backfill`, Phase
3), and, new in **Phase 4**, the review reconciliation sweep
(`POST /internal/review/reconcile`, `app/services/review_reconciliation_service.py`).
All three share one shape: bounded, resumable, per-user, idempotent, guarded
by `X-Internal-Token`. The reconciliation sweep exists because review actions
(`app/services/review_action_service.py`) write Neo4j (canonical) then
PostgreSQL (review state) in two separate, non-transactional steps — a crash
between them can leave PostgreSQL recording a decision Neo4j never actually
applied. The sweep detects that gap (comparing each `confirmed_duplicate`/
`merged` PostgreSQL row against Neo4j's actual current state) and repairs it
by re-applying the same idempotent Neo4j write, never a freshly-guessed
outcome; a row it cannot safely repair (e.g. a `merged` entity candidate with
no matching audit entry) is left alone and counted, not guessed at.

---

# 18. LLM Provider Abstraction

LLM calls should be behind a provider abstraction.

Conceptually:

```text
LLMClient
 ├── GeminiClient
 ├── OpenAIClient
 ├── AnthropicClient
 └── GroqClient
```

The application should not directly depend on provider-specific code everywhere.

---

# 19. Embedding Provider

Embedding provider should also be configurable where practical.

Do not tightly couple memory storage to a single embedding model.

Embedding dimensions and model metadata should be stored.

**Implemented in Phase 3**: `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL`/
`EMBEDDING_DIMENSION` (`.env.example`); every `memory_embeddings` row records
its own provider, model and dimension, versioned by `(memory_id, provider,
model)` rather than overwritten in place, so a model migration can run
side-by-side with the old one before cutover (ADR 0009).

---

# 20. Configuration

Use environment variables.

Never hard-code secrets.

Example:

```env
APP_ENV=development

LLM_PROVIDER=gemini
LLM_MODEL=...

DATABASE_URL=...

NEO4J_URI=...
NEO4J_USERNAME=...
NEO4J_PASSWORD=...

REDIS_URL=...

STORAGE_BUCKET=...
STORAGE_ENDPOINT=...
STORAGE_ACCESS_KEY=...
STORAGE_SECRET_KEY=...
```

---

# 21. Error Handling

AI systems fail in unusual ways.

The backend must distinguish:

* validation errors
* authentication errors
* authorization errors
* database failures
* graph failures
* LLM failures
* embedding failures
* retrieval failures
* tool failures
* document processing failures

Do not return generic:

```text
Something went wrong.
```

when a useful explanation can be provided safely.

---

# 22. Observability

Track:

```text
request ID
user ID
latency
LLM model
retrieval strategy
retrieved memories
graph operations
tool calls
token usage
errors
```

Development mode should make agent behavior inspectable.

---

# 23. Security Architecture

Security requirements:

* authentication
* authorization
* secure credentials
* user isolation
* input validation
* file validation
* rate limiting
* safe retrieval
* prompt injection resistance

**Implemented in Phase 9** (docs/API.md section 3a):
* **Authentication:** local accounts, stdlib-scrypt hashes (hashing off the
  event loop), and opaque PostgreSQL sessions (7 d absolute / 24 h sliding
  idle) with immediate revocation.
* **Browser boundary:** an `HttpOnly` `SameSite=Lax` `Secure` cookie, an
  Origin check on cookie-authenticated unsafe methods (CSRF), and
  credentialed CORS for explicit origins only.
* **Rate limiting:** PostgreSQL fixed windows, each hit in its own
  transaction, so failed requests still count. The IP is taken from the peer
  unless a trusted proxy is configured.
* **Production guards:** the startup validator refuses dev identity,
  insecure cookies, a missing origin allow-list and weak scrypt in any
  production-like environment.

---

# 24. Deployment Architecture

Preferred conceptual deployment:

```text
                Internet
                   |
                   v
              Vercel/CDN
                   |
                   v
              Next.js
                   |
                   v
             FastAPI Backend
                   |
        ┌──────────┼───────────┐
        v          v           v
   PostgreSQL    Neo4j       Redis
   + pgvector                 Queue
        |                      |
        |                      v
        |                    Worker
        |
        +----------+
                   |
                   v
             Object Storage

                   +

              LLM Provider
```

Use managed services whenever practical.

---

# 25. Deployment Principles

Do not use Kubernetes unless genuinely necessary.

The portfolio deployment should prioritize:

* reliability
* low operational overhead
* easy setup
* reasonable cost
* observability
* reproducibility

---

# 26. Development Environments

Support:

```text
Local Development
Staging
Production
```

Use separate credentials/configurations.

---

# 27. Database Migrations

Database schema changes must use migrations.

Never manually modify production schema without recording the change.

---

# 28. API Versioning

If public APIs become stable, consider:

```text
/api/v1/
```

Do not add versioning complexity prematurely.

---

# 29. Scalability

The first version should optimize for correctness rather than massive scale.

Future scaling possibilities:

* multiple workers
* caching
* graph query optimization
* vector indexing
* retrieval caching
* horizontal API scaling

Do not optimize prematurely.

---

# 30. Architecture Decision Records

Major technical decisions should be documented.

Examples:

```text
Why Neo4j?
Why PostgreSQL + pgvector?
Why FastAPI?
Why React Flow?
Why hybrid retrieval?
Why temporal memory?
Why a particular embedding model?
```

Create ADRs when a decision has meaningful long-term consequences.

---

# 31. Architecture Success Criteria

The architecture should allow:

* new LLM providers
* new retrieval strategies
* new memory types
* new document types
* new agent tools
* multiple users
* evaluation experiments
* production deployment

without requiring a complete rewrite.
