# Phase 17 — System map (M0)

What the repository **actually implements** at the end of Phase 16, taken from the code and
from running the live stack — not from earlier plans. Labels: **[V]** verified by a test or a
run, **[M]** measured, **[I]** inspected in code only, **[A]** assumption, **[L]** limitation.

## 1. Components

| Component | Where | Role |
|---|---|---|
| API | `backend/app/main.py`, `app/api/routes/*` | FastAPI app: 88 route/method pairs in 14 groups (auth, conversations, agent, documents, entities, extraction, graph, health, internal, memories, projects, project-memory, review, search) [V: `test_route_contract_inventory`] |
| Services | `backend/app/services/` | Business logic: memory CRUD, extraction orchestration, retrieval, hybrid retrieval, graph exploration/intelligence, review, documents, projects, project events, Git ingestion, project query, resume, auth, rate limiting |
| Repositories | `backend/app/repositories/` | One PostgreSQL repository per aggregate; every query is scoped by `user_id` (`UserScopedRepository`) |
| Graph layer | `backend/app/graph/` | Neo4j client, schema/constraints, Cypher repositories, all scoped by `user_id` |
| Extraction | `backend/app/extraction/` | LLM candidate extraction + deterministic governance (worthiness, duplicates, conflicts, temporal, entity resolution) |
| Agent | `backend/app/agent/` | The *internal* Memory Agent (ReAct-style loop over memory tools); not the external coding agent |
| LLM / embeddings | `backend/app/llm/`, `app/embeddings/` | Provider-agnostic clients (`LLM_PROVIDER`, `EMBEDDING_PROVIDER`), `fake` providers for tests |
| Documents | `backend/app/documents/`, `app/storage/` | Parsers, chunking, S3-compatible/local storage |
| Projects | `backend/app/projects/`, `app/services/project_*`, `git_ingest_service.py` | Project = memory space; tokens; raw agent events; Git commits; structure; queries; resume |
| Workers | `backend/app/workers/sweeper.py` | Background sweep: extraction, ingestion, project events + Git commits, sessions |
| Frontend | `frontend/` | Next.js 16 / React 19 explorer: overview, memories, entities, graph, search, documents, review, ask, settings, project switcher |
| MCP adapter | `integrations/mcp/` | Separate package (own venv); stdio MCP server with 17 high-level tools over `/projects/current/…`; the only place the `mcp` dependency exists |
| Git collector | `scripts/git_collect.py` | Local, read-only: sends commits and file structure to the engine |
| Evaluation | `backend/eval/` | Deterministic runners/benchmarks; `real_agent_continuity.py` is opt-in and was run once |

## 2. Stores

* **PostgreSQL** (+ pgvector) — users, auth sessions, conversations, messages, documents,
  chunks, extraction jobs/candidates, review candidates and audit log, ingestion jobs,
  embeddings, projects, project tokens, `agent_sessions`, `agent_events`, `git_commits`.
  Single linear Alembic chain ending at `b2f392141c1b` [V: `alembic check`/heads in the
  Phase 17 regression run].
* **Neo4j** — `Memory`, `Entity`, `Source` nodes; `MENTIONS`, `DERIVED_FROM`, `RELATES_TO`
  (free-text predicate, per-assertion `memory_id`), `SUPERSEDES`, `CONFLICTS_WITH`,
  `DUPLICATE_OF`, `MERGED_INTO`. Every node and relationship carries `user_id`.
* **Object storage** — uploaded document bytes only.

A project's memory space is its own `user_id` (`proj_<hex>`), so *all* of the above isolation
machinery applies to projects unchanged [V].

## 3. Credentials

| Credential | Issued by | Valid on | Notes |
|---|---|---|---|
| Account session (cookie or bearer from `token_in_body`) | `POST /auth/login` | all account endpoints; project endpoints for projects the account owns | optional `X-Memory-Space: <project id>` selects a space; server checks ownership |
| Project token `sbp_…` | `POST /projects/{id}/tokens` (account session) | only `/projects/{id}/…` and `/projects/current/…`, scoped read / ingest / propose | hashed at rest, expiring, revocable, shown once |
| `X-User-Id` | dev header mode only | everything | disabled outside `dev_header` auth mode |

## 4. Flows (what is stored where)

* **A. Conversation** — message → PostgreSQL → extraction job → LLM candidates → governance →
  Neo4j memory + PG embedding → retrieval/agent. Crash windows are documented in the report.
* **B. Document** — upload → storage + `documents` row → ingestion job → parse/chunk → extraction
  per chunk → same governance → memories derived from `Source(document)` with chunk provenance.
* **C. Project / coding agent** — token → `POST /events` → raw `agent_events` row (always first) →
  deterministic projection to graph (no LLM) → project queries / resume.
* **D. Git** — collector → `POST /git/commits` and `/git/structure` → `git_commits` row →
  deterministic drafts → memories with deterministic ids.
* **E. Graph explorer** — `/graph/*` and `/entities/*` read endpoints, always bounded.

## 5. Defects found and fixed in Phase 17

See `PHASE17_REPORT.md` §24. In short: MCP path traversal, concurrent Git projection losing
memories, agent write deadlock, poison events blocking a session, UI provenance gaps and a
hydration error, graph state surviving a memory-space switch, a missing CORS header.
