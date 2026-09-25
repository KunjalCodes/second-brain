# 🧠 AI Second Brain

### Persistent Graph-Based Memory for AI Agents

> **What if an AI could actually remember?**

AI Second Brain is a deployable persistent memory engine that allows AI agents to build, retrieve, reason over, and update long-term knowledge across conversations and documents.

Instead of treating knowledge as isolated text chunks, the system represents information as an evolving temporal knowledge graph combined with semantic vector retrieval.

---

# ✨ What It Does

The system can remember:

* people
* projects
* technologies
* concepts
* decisions
* events
* relationships
* historical changes

It can then answer questions such as:

> What projects have I worked on involving OCR?

> What was I using in March?

> What changed in my project architecture?

> How are these two concepts connected?

> Where did I mention this?

---

# 🧠 Example

Suppose a user says:

> I'm building a drawing extraction engine using EasyOCR and Gemini.

The system creates:

```text
             Drawing Extractor
                /          \
               /            \
          uses               uses
           /                   \
       EasyOCR                Gemini
```

Later:

> We switched from EasyOCR to PaddleOCR.

The system preserves history. One `uses` relationship per fact, each with its own
validity window — the old edge is closed, never deleted:

```text
Drawing Extractor
       |
       +── uses ──> EasyOCR     valid: January → May   (superseded)
       |
       +── uses ──> PaddleOCR   valid: May → now       (active)
```

`previously_used` / `currently_uses` are then *derived* from those windows at query
time. Keeping the window rather than rewriting the predicate is what makes the
historical question answerable — see
[ADR 0003](docs/adr/0003-relationship-representation.md).

The AI can now answer all three:

> What do I use now?

> What did I use before?

> What was I using in March?

---

# 🚀 Core Capabilities

* Persistent AI memory
* Temporal knowledge graph
* Entity resolution
* Relationship extraction
* Conflict detection
* Memory updates
* Hybrid graph + vector retrieval
* Multi-hop retrieval
* Agentic memory tools
* Provenance tracking
* Document ingestion
* Evaluation framework
* Multi-user authentication
* Production deployment

---

# 🏗️ Architecture

```text
                         USER
                           |
                           v
                    Next.js Frontend
                           |
                           v
                     FastAPI API
                           |
                ┌──────────┼──────────┐
                |          |          |
                v          v          v
             Agent      Memory     Documents
                |        Engine       |
                |          |          |
                |          v          |
                |      Retrieval      |
                |        Engine       |
                |          |          |
                |    ┌─────┴─────┐    |
                |    v           v    |
                |  Vector       Graph |
                |  Search       Search|
                |    |           |    |
                |    └─────┬─────┘    |
                |          v           |
                |       Reranker       |
                |          |           |
                └──────────┼───────────┘
                           v
                          LLM
```

---

# 🛠️ Technology

## Frontend

* Next.js
* TypeScript
* Tailwind CSS
* shadcn/ui
* React Flow / Cytoscape.js

## Backend

* Python
* FastAPI
* Pydantic

## Data

* PostgreSQL
* pgvector
* Neo4j
* Redis
* S3-compatible object storage

## AI

Configurable LLM providers:

* Gemini
* OpenAI
* Anthropic
* Groq

---

# 🔍 Why Graph + Vector?

Traditional RAG is excellent for semantic similarity.

But some questions require relationships.

For example:

> Which project used the technology that replaced the OCR library I was using previously?

This requires:

```text
Project
 ↓
OCR Library
 ↓
Replacement
 ↓
Technology
```

Graph retrieval can represent this structure.

Vector retrieval can provide semantic context.

The system combines both.

---

# ⏳ Temporal Memory

The system does not treat memory as static.

Example:

```text
January
EasyOCR

May
PaddleOCR

June
OpenCV
```

The system should understand:

```text
Current:
PaddleOCR

Historical:
EasyOCR
```

---

# 🤖 Agent Tools

The agent can interact with memory using tools such as:

```text
search_memory()
get_entity()
traverse_graph()
get_memory_history()
search_documents()
get_source()
create_memory()
update_memory()
invalidate_memory()
```

This allows the agent to actively retrieve and manage its own memory.

---

# 📚 Document Ingestion

The system can eventually learn from:

* PDF
* Markdown
* TXT
* DOCX
* URLs

Documents are processed into:

```text
Document
 ↓
Text
 ↓
Entities
 ↓
Relationships
 ↓
Embeddings
 ↓
Knowledge Graph + Vector Store
```

---

# 📊 Evaluation

A major part of the project is measuring whether persistent memory actually improves AI performance.

The system compares:

```text
LLM Only
Vector RAG
Graph RAG
Hybrid Graph + Vector
Hybrid + Temporal Memory
```

Metrics include:

* Recall@K
* Precision@K
* MRR
* NDCG
* entity accuracy
* relationship accuracy
* temporal accuracy
* answer correctness
* citation accuracy
* tool-use efficiency
* latency
* token usage

---

# 🧪 Failure Analysis

The project explicitly documents failure cases such as:

* incorrect entity resolution
* incorrect relationships
* outdated memory retrieval
* missed conflicts
* temporal reasoning errors
* irrelevant graph traversal
* incorrect tool selection
* unsupported answers

The goal is not to pretend the system is perfect.

The goal is to understand where AI memory systems fail.

---

# 🖥️ Product Interface

The application contains:

### Chat

Interact with the AI.

### Knowledge Graph

Explore relationships.

### Memory Explorer

Inspect stored memories.

### Entity Explorer

Understand individual entities and their history.

### Source Explorer

Trace memories back to their origin.

---

# 🔐 Security

The deployed application supports:

* authentication
* authorization
* user-level memory isolation
* secure API keys
* input validation
* rate limiting
* document security
* prompt injection defenses

---

# 🚀 Quick Start (Phase 1 + Phase 2 + Phase 3 + Phase 4 + Phase 5)

Memories can be created, stored in Neo4j, retrieved, updated, invalidated and
traversed (Phase 1). Conversations and messages are persisted in PostgreSQL, and an
LLM can propose memory candidates that a deterministic decision engine reviews
before anything reaches Neo4j (Phase 2). Every persisted memory is embedded
(PostgreSQL + pgvector) and searchable via hybrid graph + vector + keyword
retrieval, with semantic similarity as evidence only — never an automatic merge
(Phase 3). Retrieval now traverses up to 3 hops with real reported paths, and
flagged duplicates/conflicts/entity-merge candidates go through an explicit,
reversible, audited human review workflow rather than any automatic merge
(Phase 4). A persistent-memory agent (`POST /conversations/{id}/agent-turn`)
reasons over the same retrieval/memory services through a bounded, custom
tool-calling loop, with citations structurally checked against what was
actually retrieved and its one write tool feature-gated off by default
(Phase 5). **No Docker required**, and everything — including extraction,
semantic search, and the agent — runs with **no API key** by default
(`LLM_PROVIDER=fake`, `EMBEDDING_PROVIDER=fake`).

```bash
# 1. Neo4j — free managed Aura, or locally:
brew install neo4j
neo4j-admin dbms set-initial-password <password>   # before first start
brew services start neo4j

# 2. PostgreSQL (Phase 2/3) — free managed Supabase/Neon, or locally.
# pgvector must be installable as an extension (standard on all of the above):
brew install postgresql@17 && brew services start postgresql@17
createdb ai_second_brain

# 3. Configure
cp .env.example .env     # set NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD, DATABASE_URL

# 4. Install
cd backend && uv venv && uv pip install -e ".[dev]"

# 5. Apply schemas (all idempotent)
uv run python ../scripts/init_graph.py      # Neo4j: constraints and indexes
uv run alembic upgrade head                 # PostgreSQL: conversations, extraction,
                                             #   the `vector` extension, memory_embeddings

# 6. Run
uv run uvicorn app.main:app --reload --port 8000    # → http://localhost:8000/docs

# 7. Optional: seed the Phase 1 demo dataset
uv run python ../scripts/seed_demo.py

# 8. Tests — no API key, no network, no cost
uv run pytest tests/unit -q   # 452 tests, no database needed
uv run pytest -q              # everything; integration skips without services

# 9. Optional: the evaluation harnesses (real pipeline, Fake clients)
uv run python eval/runner.py             # extraction decision accuracy
uv run python eval/retrieval_runner.py   # Recall@K/Precision@K/MRR, hybrid vs. single-channel
```

CI runs Ruff, the unit suite, and the Cypher/SQL safety audits on every push, with
no database and no Docker required. Integration tests are a separate, opt-in
workflow that needs Neo4j and PostgreSQL — see
[docs/API.md](docs/API.md#continuous-integration).

Full reference — data model, graph schema, every endpoint, the extraction and
retrieval pipelines, all environment variables — is in **[docs/API.md](docs/API.md)**.
Architecture decisions are in **[docs/adr/](docs/adr/)**; the full Phase 2 and
Phase 3 designs are in **[docs/PHASE2_PLAN.md](docs/PHASE2_PLAN.md)** and
**[docs/PHASE3_PLAN.md](docs/PHASE3_PLAN.md)**.

---

# ☁️ Deployment

The target deployment architecture uses managed infrastructure.

Example:

```text
Frontend
    ↓
Vercel

Backend
    ↓
Cloud Run / Railway / Render / equivalent

Database
    ↓
Managed PostgreSQL

Graph
    ↓
Managed Neo4j

Storage
    ↓
S3 / R2 / equivalent
```

The application is designed to be deployable without requiring Kubernetes.

---

# 📁 Repository Structure

```text
ai-second-brain/
│
├── backend/                     FastAPI service (Phase 1 + Phase 2 — implemented)
│   ├── app/
│   │   ├── main.py              entry point: app.main:app
│   │   ├── core/                config, error taxonomy, logging
│   │   ├── graph/               Neo4j connection, schema, serialization
│   │   ├── db/                  PostgreSQL engine + SQLAlchemy models (Phase 2)
│   │   ├── models/              domain models, enums, lifecycle rules
│   │   ├── schemas/             request/response schemas
│   │   ├── repositories/        all Cypher/SQL lives here
│   │   ├── services/            business logic
│   │   ├── llm/                 provider-independent LLM client + FakeLLMClient (Phase 2)
│   │   ├── extraction/          gate, grounding, decision engine, runner (Phase 2)
│   │   └── api/                 routes and dependencies
│   ├── migrations/              Alembic migrations for PostgreSQL (Phase 2+)
│   ├── eval/                    evaluation harnesses + fixtures (Phase 2-7; see docs/PHASE7_PLAN.md)
│   ├── tests/
│   │   ├── unit/                no database required
│   │   └── integration/         requires Neo4j + PostgreSQL; skips when absent
│   ├── pyproject.toml
│   └── Procfile
│
├── frontend/                    Next.js on Vercel (Phase 10 per the roadmap — not built yet)
│
├── docs/
│   ├── PRODUCT.md
│   ├── ARCHITECTURE.md
│   ├── EVALUATION.md
│   ├── API.md                   data model, graph schema, extraction pipeline, endpoints
│   ├── PHASE2_PLAN.md .. PHASE7_PLAN.md   per-phase design + implementation status
│   └── adr/                     architecture decision records (0001-0017)
│
├── scripts/
│   ├── init_graph.py            apply Neo4j constraints and indexes
│   └── seed_demo.py             deterministic demo dataset
│
├── .env.example
├── CLAUDE.md
└── README.md
```

Tests live under `backend/tests/` rather than at the repository root, so that
`pytest` and the `app` package resolve without path manipulation and the backend
stays independently deployable.

---

# 🗺️ Development Roadmap

## Phase 1

Graph memory MVP.

## Phase 2

Persistent conversation memory.

## Phase 3

Hybrid retrieval.

## Phase 4

Temporal memory + conflict resolution.

## Phase 5

Agent + memory tools.

## Phase 6

Document ingestion.

## Phase 7

Evaluation framework.

## Phase 8

Memory graph exploration API (bounded, read-only backend contract for the Memory Explorer).

## Phase 9

Authentication + productionization.

## Phase 10

Memory Explorer UI + deployment.

---

# 🎯 Project Goal

The goal is not to build:

> Another AI chatbot.

The goal is to explore:

> **How persistent structured memory can give AI agents continuity across time.**

---

# 🔬 Research Questions

The project explores:

* Does graph memory improve multi-hop retrieval?
* Does temporal memory improve historical questions?
* Does hybrid retrieval outperform vector-only retrieval?
* Can an agent reliably manage its own memory?
* How should conflicting memories be handled?
* How should AI memories be evaluated?
* What are the latency and cost tradeoffs of structured memory?

---

# 📌 Status

**Phase 9 complete — authentication and session security (server-side PostgreSQL sessions, CSRF/CORS boundary, rate limiting, production-safety guards) on top of the Phase 8 memory graph exploration API.**

| Phase | Scope | Status |
|---|---|---|
| 1 | Graph memory foundation (Neo4j, temporal memory, provenance, traversal) | ✅ Implemented |
| 2 | Persistent conversation memory + LLM extraction | ✅ Implemented |
| 3 | Semantic memory + hybrid graph/vector retrieval | ✅ Implemented |
| 4 | Multi-hop graph retrieval + memory/entity review workflow + threshold calibration + scale evaluation | ✅ Implemented |
| 5 | Persistent-memory agent: custom tool-calling loop, 7 memory tools (6 read + 1 gated write), citation/temporal/provenance enforcement, evaluation harness, real-LLM behavioral validation | ✅ Implemented and validated |
| 6 | Document ingestion: TXT/Markdown/text-layer PDF, deterministic chunking, provenance, extraction-pipeline reuse (no second decision engine), zero-cost local storage | ✅ Implemented |
| 7 | Evaluation framework: persistent/resumable evaluation runs (PostgreSQL), generalized `retrieval_necessity` category, strengthened evaluation-only baseline B, real-LLM checkpoint/resume | ✅ Implemented — see `docs/PHASE7_PLAN.md` for measured results and known limitations |
| 8 | Memory graph exploration API: `/graph/overview`, `/graph/entities/{id}/neighborhood`, `/graph/edges/evidence`, `/graph/memories/{id}/timeline` — one `GraphSlice` shape, server-side budgets with exact truncation flags, current-by-default temporal scope, merge-aware, user-isolated; measured caps on the existing `/entities/{id}/graph` | ✅ Implemented — see `docs/PHASE8_PLAN.md` section 25 for measured results and known limitations |
| 9 | Authentication & productization foundation: email/password accounts, opaque server-side sessions (HttpOnly cookie or Bearer), logout/logout-all/password change, CSRF Origin check and narrowed CORS, PostgreSQL rate limiting, production startup guards, operator CLI for legacy ids | ✅ Implemented — see `docs/PHASE9_PLAN.md` section 24 for measured results and known limitations |
| 10 | Memory Explorer UI + deployment | Not started |

Current test count and per-phase measured evaluation results are reported in the latest phase plan's implementation-status section (`docs/PHASE9_PLAN.md` section 24) rather than duplicated here, since they change with every phase.

Phase 1 deliberately contains **no LLM, no embeddings, no agent, no document
ingestion and no authentication**, so the graph-memory core could be verified on
its own. Phase 2 adds conversation persistence and LLM-proposed memory extraction
on top of that foundation, keeping the same discipline: extraction is
conservative by design (see [ADR 0007](docs/adr/0007-llm-proposes-application-decides.md)) —
the LLM proposes candidates, a deterministic decision engine decides, and Neo4j
remains the sole canonical memory store. Phase 3 adds embeddings (PostgreSQL +
pgvector) and hybrid graph/vector/keyword retrieval on top of that (see
[ADR 0009](docs/adr/0009-pgvector-for-embeddings-neo4j-remains-canonical.md)) —
semantic similarity is evidence for the same conservative decision engine, never
a merge/overwrite decision-maker by itself, and Neo4j's temporal/status state is
always re-verified at retrieval time rather than trusted from a PostgreSQL copy.
The whole system, including semantic duplicate detection and vector search, runs
by default with **no LLM or embedding API key** via `FakeLLMClient`/
`FakeEmbeddingClient`. Exact-triple duplicate detection is now supplemented, not
replaced, by semantic evidence (`LIKELY_DUPLICATE`/`FLAGGED`, never auto-merged) —
see `docs/PHASE3_PLAN.md` for what remains deliberately out of scope (a learned
reranker, an LLM query planner, multi-hop graph relevance decay).

Phase 4 adds bounded multi-hop graph retrieval (default 2 hops, hard max 3,
exponential relevance decay, real traversed paths reported — never fabricated)
and memory governance: a review workflow for confirming/rejecting semantic
duplicates, resolving conflicts, and approving/reversing entity merges (see
[ADR 0010](docs/adr/0010-memory-entity-consolidation-via-redirect.md)) — a
confirmed duplicate or merge is never a deletion, only an additional
`DUPLICATE_OF`/`MERGED_INTO` redirect relationship, reversible for entity
merges, and every canonical mutation writes an append-only audit trail before
PostgreSQL records the review outcome, with a reconciliation sweep closing the
narrow (no-distributed-transaction) gap if a crash lands between the two. A
dense-graph benchmark at 100/1,000/10,000 synthetic nodes found the real
scaling bottleneck was a missing Neo4j index, not graph fan-out — see
`docs/PHASE4_PLAN.md` and `docs/EVALUATION.md` for the measured numbers and
what remains deliberately out of scope (an autonomous agent, tool-calling,
answer generation, a learned reranker, automatic semantic merging).

Phase 5 adds the agent itself: a small custom tool-calling loop built on the
same `LLMClient.complete_structured` primitive extraction already uses (no
LangGraph, no second LLM abstraction), six read-only memory tools plus one
write tool (`propose_memory_update`, feature-gated by `AGENT_WRITE_ENABLED`
and reusing the extraction pipeline verbatim — see
[ADR 0011](docs/adr/0011-agent-writes-reuse-the-extraction-pipeline.md)),
structural citation validation (a final answer may only cite a memory id a
tool call actually returned this turn), and a `POST
/conversations/{id}/agent-turn` endpoint. `eval/agent_runner.py` compares
five baselines (no memory, vector-only, graph-only, hybrid, and the agent
loop itself), the last of which is measured twice — `E_scripted_mechanics`
(tool-selection/citation scripted, proves the loop's plumbing only) and
`E_real_llm` (every decision made by the real, configured Gemini model —
88.9% mean recall / 100% mean precision across all 9 evaluation tasks). See
`docs/PHASE5_PLAN.md`'s implementation-status section for the full per-task
results, the two genuinely imperfect outcomes reported honestly rather than
hidden, the three real Gemini structured-output compatibility bugs found and
fixed along the way, and what remains deliberately out of scope (document
ingestion, streaming responses, a second memory store).

The architecture and feature set are intentionally iterative.

---

# 📜 License

To be decided.
