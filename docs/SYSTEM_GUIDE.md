# System guide — what this product is and how it works

For a strong engineer new to the repository. Read this first, then `SYSTEM_ARCHITECTURE.md`
(diagrams), `PHASE17_SYSTEM_MAP.md` (inventory) and `PHASE17_REPORT.md` (audit).
Claims carry the same labels as the report: **[V]** verified, **[I]** inspected, **[L]** limitation.

## 1. What this is

A **persistent, graph-based memory engine for AI agents**. It is not a chatbot, not a PDF Q&A
app and not a graph viewer. Information from conversations, documents, Git history and coding-agent
sessions becomes **memories** (statements with provenance, confidence, status and validity
interval) linked to **entities** and **relationships** in a graph. Agents and people read it back
through retrieval, a graph explorer, and (for coding agents) an MCP tool surface.

Two kinds of memory space exist, with one engine behind both:

* **Personal memory** — your account's own memory (conversations, documents).
* **Project memory** — one space per software project; holds sessions, decisions, bugs, fixes,
  commits and file structure. A project *is* a memory space: it has its own `user_id`, so every
  isolation rule applies to it unchanged. [V]

## 2. What the user can do

Register/sign in; chat and have durable facts extracted; upload documents; browse memories,
entities, a bounded graph, hybrid search; inspect *why* a memory exists (provenance, extraction
decision); review ambiguous merges/duplicates/conflicts; ask the internal Memory Agent; create
projects, issue scoped tokens, switch the explorer between spaces.

## 3. What a coding agent can do

Through MCP (17 tools) or REST with a project token: resume the last work, record events
(decisions, errors, fixes, tests, commits, blocked tasks), look up file/decision/bug/fix/attempt
history, trace a memory to its stored source, and propose a bounded, governed memory update. The
adapter exposes no SQL, Cypher, shell or filesystem, and no way to name a user or project.

## 4. Where data lives

PostgreSQL holds process state and raw records (messages, documents, chunks, jobs, review queue,
audit log, embeddings, projects, tokens, agent events, Git commits). Neo4j holds knowledge
(Memory/Entity/Source nodes and their relationships). Object storage holds uploaded bytes only.
There is **no distributed transaction**; write order and deterministic ids make crashes
recoverable [V for events/Git; L elsewhere, see report §17].

## 5. Core definitions

* **Memory** — one statement: `content, type (episodic/semantic/procedural/working), entities,
  relationships, source, confidence, created_at, updated_at, valid_from, valid_until, status`.
* **Entity** — identifiable thing (person, project, technology, …; 14 project-specific types
  such as FILE, COMMIT, DECISION, BUG, ERROR, FIX).
* **Relationship** — `RELATES_TO` with a free-text predicate and the `memory_id` that asserts it;
  project predicates are multi-valued.
* **Source** — where a memory came from: message, document(+chunk), URL, user statement, tool
  result, system inference, **git_commit**, **agent_event**.
* **Status** — active, historical, superseded, invalid, conflicting, archived, merged.
* **Confidence** — metadata from the extractor, not a truth value. Never ranks alone.

## 6. Flow: conversation

Message committed → extraction job queued → the sweeper (or an immediate attempt) claims it →
an LLM proposes candidates → deterministic governance decides what is worth keeping, resolves
entities, checks duplicates/conflicts/time → memory written to Neo4j with provenance, embedding
stored in PostgreSQL. Not every sentence becomes memory: worthiness and temporal checks filter
chatter. Ambiguity (possible duplicate, conflict, uncertain merge) creates a **review candidate**
rather than a silent decision. [V]

## 7. Flow: document

Upload → stored bytes + `documents` row → ingestion job → parse/clean/chunk → per-chunk
extraction → the same governance → memories whose provenance points to the exact chunk. Document
text is **untrusted data**; instructions inside it are content, never commands. [V]
**[L]** A crash while a document is EXTRACTING leaves it in that state until reprocessed.

## 8. Flow: project / coding agent

Token → event → **raw event row written first** → deterministic projection to the graph (no LLM) →
queryable and resumable. A projection that fails stays `pending` and is retried by the
sweeper; after `PROJECT_PROJECTION_MAX_ATTEMPTS` it is `failed` and **no longer blocks later events
of its session** (Phase 17). Memory ids are `uuid5(space, event)`, so reprocessing adopts the
existing memory instead of duplicating it. [V]

## 9. Flow: Git

`scripts/git_collect.py` reads local history read-only and sends commits and file structure.
Commit rows are idempotent on SHA; commit/file memories have deterministic ids. Concurrent
requests for the same commit race on memory creation; the loser now **adopts** the winner's
memory (Phase 17 fix; previously the commit could be dropped as `skipped`). [V]

## 10. Retrieval

See `RETRIEVAL_ARCHITECTURE.md`. Personal memory: hybrid (vector + graph + keyword) with
temporal scopes. Project memory: structural SQL/graph retrieval with deterministic ranking, no LLM.
There is **no cross-space search** (documented design limit).

## 11. Temporal behaviour

Intervals are half-open: `valid_from` inclusive, `valid_until` exclusive. CURRENT returns what is
true now; HISTORICAL what is no longer true; AS_OF a given instant; UNSPECIFIED does not filter
by time and labels what it returns. Superseding **closes** the old memory (`valid_until`,
status `superseded`) and links it; it never overwrites it, so "what do I use now?" and "what was
I using in March?" give different, correct answers. [V: temporal tests]

## 12. Governance: what can write memory

| Source | Passes through | Notes |
|---|---|---|
| Conversation / document extraction | full governance + LLM candidates | review queue for ambiguity |
| Internal agent `propose_memory_update` (needs `AGENT_WRITE_ENABLED`) | governance, user-scoped | off by default |
| Direct `POST /memories` | validation, optional embedding | explicit user/API action |
| Project event | `apply_structured_candidate` (duplicate/conflict/temporal; no worthiness, no LLM) | deterministic |
| Git commit / structure | deterministic drafts, adopt-existing | evidence label `git` |
| MCP `propose_project_memory_update` | same structured path, confidence ≤ 0.8, label `agent_stated` | never treated as fact from Git |

**[L]** Project-event and Git memories do not enter the review queue, and "why remembered" shows
"direct" for them (the UI now says so accurately).

## 13. Provenance

Every memory traces to a `Source`; the UI shows the stored original (message, document chunk,
Git commit, agent event). Provenance is stored, not generated. `get_source` (MCP) and
`/projects/{id}/sources/...` return the raw record.

## 14. Authentication and isolation

Account sessions (cookie) for the browser; scoped `sbp_` project tokens for agents (read/ingest/
propose, hashed, expiring, revocable, valid only under their own project). Isolation is enforced
by `user_id` in every SQL row and Cypher statement, never by the frontend. Project A cannot read
project B; user A cannot reach user B's projects (404, not 403) [V: isolation and auth-matrix tests,
live matrix].

## 15. The two agents

* **Internal Memory Agent** (`/conversations/{id}/agent-turn`) — an LLM loop over memory tools for
  *the user's own* chat. Read-mostly; writes only when enabled.
* **External coding agent** — any MCP/REST client (e.g. Claude Code). It is *not* part of the
  engine and the engine never calls it. The engine only stores what it reports and what Git says.

## 16. Resume (continuity)

`get_resume_context` returns unfinished work **only from explicit evidence** (a blocked task, a
failed attempt, a feature started but not completed, a failing test with no later pass). It
never infers intent from silence. Relevance-gated by default; `task` biases but does not filter.
When no evidence exists the answer is "nothing to resume". [V]

## 17. What is deliberately not done

No cross-space search; no auto-merging of ambiguous entities; no deletion of history; no
inference of unfinished work or failed attempts; no raw SQL/Cypher or filesystem access through
MCP; no unbounded graph reads; no requirement for any particular LLM vendor for MCP.

## 18. Where to look in the code

| Question | Start here |
|---|---|
| How is a route authorised? | `backend/app/api/deps.py` (`get_current_user_id`: session/token resolution and `X-Memory-Space`) |
| How does a memory get created from text? | `app/extraction/runner.py` (`ExtractionRunner`), `trigger.py`, `gate.py` |
| What decides duplicate/conflict/supersede? | `app/extraction/` (`worthiness.py`, `resolution.py`, `decisions.py`, `temporal_hints.py`) and `app/semantic/similarity.py` |
| How are project events projected? | `app/services/project_event_service.py`, `app/projects/projection.py` |
| How is Git ingested? | `app/services/git_ingest_service.py`, `app/projects/git_projection.py`, `scripts/git_collect.py` |
| How do queries answer "what changed in file X"? | `app/services/project_query_service.py` |
| How does resume decide? | `app/services/project_resume_service.py` |
| What does an MCP tool call? | `integrations/mcp/memory_engine_mcp/server.py` |
| What is tested how? | `PHASE17_EVALUATION_MAP.md` |

## 19. Running it

See `docs/DEPLOYMENT.md` and the root README: backend `uvicorn app.main:app`, Neo4j and PostgreSQL
configured by environment, frontend `npm run dev` / `next start`. Never commit `backend/.env`.
