# Phase 16 — Architecture (M0 audit and design; as built, §13)

**Status:** approved and implemented; §13 lists where the build differs from this design. Baseline `510456c`.
Evidence labels: [I] inspected in code, [A] assumption/proposal, [L] limitation.

## 1. Current architecture [I]

```text
user → conversation → message ──gate──▶ extraction_jobs (Postgres, lease-owned)
                                          │  ExtractionRunner (LLM → CandidateMemory)
                                          ▼
   worthiness → temporal hints → grounding → entity flagging → classify
   (NEW/UPDATE/DUPLICATE/SUPERSEDING/CONFLICTING/FLAGGED/LIKELY_DUPLICATE)
                                          ▼
   MemoryService.create / attach_source  → Neo4j: Memory, Entity, Source,
   (one transaction)                        MENTIONS, DERIVED_FROM, RELATES_TO,
                                            SUPERSEDES, CONFLICTS_WITH, ...
   embed_and_store_memory → memory_embeddings (pgvector)
   review candidates → memory_review_candidates (human review, merges)
retrieval: HybridRetrievalService (vector + graph + keyword → rank) → agent tools,
           /search, /graph/*, Phase 15 intelligence services → Explorer UI
provenance: Source node (reference, location) → ProvenanceService.resolve_sources
            (hard-coded branches for message and document)
```

Facts that shape Phase 16:

| Fact | Consequence |
|---|---|
| Isolation is `user_id` everywhere (SQL base class, audited Cypher), nothing else | A project scope is either a new key on every row or a reuse of `user_id` |
| `EntityType` is a closed enum (10 values), enforced by pydantic only; Neo4j stores strings | New types need no Neo4j change, but need schema regeneration and an extraction-prompt decision |
| Predicates are free text; edges are one per assertion with `memory_id` | Project relationships need no model change |
| `SourceType` has `tool_result`/`system_inference`; only message and document are produced and resolved | New source types need a resolver branch and, if Postgres-backed, FK columns |
| `_apply_candidate` / `_classify_and_persist` / `_persist` are source-agnostic | A deterministic (no-LLM) producer can reuse the governance pipeline |
| Read paths (`/search`, `/graph/*`, Phase 15) use no LLM | Provider-agnostic reads already hold |
| Auth: bearer `sbs_` session tokens work for non-browser clients, but are full-rights, 7-day, and re-login needed | A scoped, durable machine credential is new work |
| No MCP code or dependency; `mcp` 2.2.0 installs cleanly (dry run) | Use it only in a separate adapter package |
| Retrieval has no scope filter beyond `user_id` and temporal intent | Project scope must come from the data partition, not a filter |

## 2. New integration boundary [A]

```text
External coding agent (own model / provider / API)
        │  MCP (stdio)
        ▼
integrations/mcp/   ── thin adapter, `mcp` SDK only here, no user_id/project_id in any tool
        │  HTTPS + Authorization: Bearer sbp_… (project token)
        ▼
FastAPI  /projects/…  ── authenticate → authorize → bound → validate
        │
        ▼
Project services (deterministic, no LLM): search, history, file, commit, decisions,
bugs, attempts, session, resume, propose, source        reuse Hybrid + Phase 15
        │
        ├── PostgreSQL: projects, project_tokens, agent_sessions, agent_events,
        │                git_commits (+ existing tables)
        └── Neo4j: same Memory/Entity/Source graph, in the project's space
```

The adapter holds no state and no business logic. Everything it can do is a REST
call a person could make with the same token.

## 3. Project scope [A]

**Decision D1 (recommended): a project is a memory space** (as proposed in
`PHASE12_PLAN.md` §4). Each project has its own `users` row (`space_id`), owned by
the account. Every repository, Cypher statement and audit already filters on
`user_id`, so passing `space_id` through the same seam gives isolation by
construction with no repository change, and entity resolution is per project.
`projects.owner_user_id` plus an ownership check on every request gives the
user+project enforcement the spec asks for.

Rejected: a `project_id` property on every row. It touches every statement, every
index and every audit, and one missed filter leaks across projects.

Cost, stated plainly [L]: a user's personal memory and a project's memory are
separate spaces. Cross-space search is not provided.

**Resolution per request:**
- MCP/REST machine client: token → `project_tokens` row → project → owner and
  `space_id`. The client cannot name another project.
- Browser: cookie → account; `X-Memory-Space: <project_id>` → ownership check
  (404 if not owned) → `space_id`.
- Without either, existing behaviour is unchanged (the account's own space).

## 4. Project identity (M3) [A]

- `project_id`: server-generated UUID. Never derived from a path.
- Repository identity key: SHA-256 of the normalized remote URL (lowercased host,
  strip credentials, `.git`, trailing slash, `ssh`/`https` unified to `host/owner/repo`).
  Without a remote: the root commit hash(es). Unique on `(owner_user_id, identity_key)`.
- Registration is idempotent: the same identity returns the existing project.
- The local path is never stored. An optional display name is accepted.
- Normalization rules are documented and unit-tested, including hostile input.

## 5. Graph model (M2) [A]

No new labels or relationship types. Additions only:

- `EntityType` values (additive): repository, branch, directory, file, code_symbol
  (class/function/method distinguished in the name prefix), dependency, feature,
  bug, agent_session, commit, change, error, fix, test_run. `decision` exists.
- Deterministic names, exact identity (excluded from fuzzy duplicate flagging via an
  `EXACT_IDENTITY_TYPES` set): `file:<normalized path>`, `commit:<sha>`,
  `session:<uuid>`, `branch:<name>`, `symbol:<path>::<qualified name>`.
- Predicate reference set (free text, validated by the existing pattern):
  contains, depends_on, calls, changed, implemented, fixed, worked_on, modified,
  inspected, discovered, made, encountered, fixed_by, led_to, superseded_by,
  validated, affected.
- **Only evidenced edges are persisted.** A relationship exists only because an
  event, a commit or a parser produced it, and carries its source.

Volume control: one memory per commit with up to N relationship edges (the model
already supports many edges per memory), one per session event, and one per file for
structure. Structural memories are keyword/graph-retrievable and skip embeddings by
default to keep ingestion cost bounded; this is a proposal to confirm.

## 6. Provenance (M24) [A]

New `SourceType` values: `git_commit` (reference = `git_commits.id`, location
`file:<path>`) and `agent_event` (reference = `agent_events.id`, location
`field:<name>`). `ProvenanceService` gains one resolver branch each, returning
commit hash/message/author/date or the event type/session/time. A parser-derived
structural fact uses `git_commit` (the snapshot it was read from). An agent claim is
always `agent_event`, never `git_commit`. Every project response labels each item
`evidence`: `git` | `agent_stated` | `parser` | `derived`.

## 7. Ingestion and write flows

**Git (M4, M5).** A local collector `scripts/git_collect.py` runs `git` read-only
(fixed argument lists, no shell), and pushes bounded batches of commits (hash,
parents, author name/email hash, dates, message, changed files with status and
add/delete counts; no diffs by default) plus a structure snapshot (directories, files,
language, Python classes/functions/imports via stdlib `ast`; other languages file-level
only). The server never reads the filesystem. Idempotency key: commit sha.

**Agent events (M6–M8, M26).**
```text
event → size/schema validation → server stamps user/project/session (never client)
      → append to agent_events (unique (session_id, event_key); duplicate = no-op)
      → deterministic normalization → memory candidate
      → entity resolution (exact for structural types) → duplicate/conflict check
      → classify → persist (same governance as extraction) → provenance agent_event
```
Raw event first (Postgres, canonical for ordering), then graph projection, with a
`projection_status` column and a retry sweep. Memory ids are derived
deterministically from the event id so a retry cannot duplicate (needs
`MemoryService.create` to accept a caller id: to verify). Neo4j down ⇒ the event is
accepted and projected later (eventual consistency, documented); Postgres down ⇒ the
request fails and nothing is written.

**Proposals (M25).** `propose_project_memory_update` takes a structured statement,
entities and relationships (no LLM), validates, and runs the same classify/persist.
Conflicts and flagged items land in the existing review queue; they never overwrite.

## 8. Retrieval, history, resume

- Search: `HybridRetrievalService` over the project's space, with Phase 15
  `context_subgraph` for connected results. No second engine.
- History, file, commit, decision, bug, attempts: deterministic services over
  `agent_events`, `git_commits` and the graph, ordered by **recorded time** and
  showing **valid time** separately.
- **Decision vs fact:** decisions carry `evidence: agent_stated`; a rationale is
  returned only if the event supplied one (`EXPLICIT_DECISION`); anything else is
  labelled `INFERRED_REASON` or `OBSERVED_OUTCOME` with its basis.
- **Previous attempts:** only from explicit event outcomes (`failed`/`partial`/
  `succeeded`) and explicit `ERROR → FIX` links. File changes alone never imply a
  failed attempt.
- **Resume (M15–M17):** one bounded call. Ranking (heuristic, documented, returned
  with signals): unfinished work → latest relevant session → recent decisions →
  unresolved bugs → recent errors → recent file changes → commits → supporting
  memories. "Unfinished" is only an unclosed `USER_REQUEST` or an explicit
  `TASK_BLOCKED`, labelled with its basis. Hard caps per section; continuation tokens
  for more.

## 9. Authentication and authorization (M21) [A]

- New `project_tokens` (`sbp_` + 43 chars, SHA-256 stored, scopes
  `read|ingest|propose`, expiry, revoked_at, bound to one project). Bearer only;
  refused on `/auth/*`. Existing `extract_credential` gains a prefix branch.
- Every project route: resolve credential → project → owner → scope check → rate
  limit (keyed on the owner's account) → handler. Session ownership is checked
  against the project; a session id from another project is a 404.
- Tool arguments never contain `user_id`, `project_id`, ownership or database ids
  other than ids the server returned earlier (and those are re-authorised).
- Expired/revoked/forged tokens: 401; wrong project: 404.

## 10. Security boundaries (M31) [A]

No Cypher, SQL, shell, filesystem or environment access from any tool. Path inputs
are normalized relative paths (reject `..`, absolute, NUL, over-length). Payload and
field size caps. All stored text returned to an agent is marked untrusted
(`untrusted: true` on free-text fields); the adapter never executes or interprets it.
Commit messages, event text and file names are treated as hostile input in tests.

## 11. UI (M23) [A]

Extend the existing Explorer only: project switcher (space), new entity types in the
filter and distinct per-type styling, session/date filters, and the existing path and
explanation panels working over the new types. All Phase 8 budgets still apply.

## 12. Provider-agnostic (M20) [A]

No new code path calls an LLM. The MCP tools, ingestion and resume are deterministic.
Existing LLM extraction is untouched. No Gemini setting is needed to run any of it.

## 13. As built: deviations from the proposal

| Proposal | As built |
|---|---|
| "`MemoryService.create` accepting a caller id: to verify" | Verified **not** safe: `id_fn` also generates entity and source ids and collided on the first entity. Added an explicit optional `memory_id` (default unchanged); `Memory.id` global uniqueness is the backstop [V] |
| Governance for all projected facts | Agent claims use the full classify/persist pipeline (`apply_structured_candidate`). Immutable Git/parser evidence uses deterministic ids and exact identity without classification (a commit hash cannot conflict) |
| Conflicts "land in the existing review queue" | Agent-stated conflicts are stored with status `conflicting` and linked, never overwriting; the review queue is bound to extraction jobs, so they are **not** queued [L] |
| Event types: 13 | 13 + `memory_proposal` (for `propose_project_memory_update`) |
| Structure parsing: Python via `ast` | As proposed; other languages file-level |
| Resume: ranked sections | Added a relevance gate (default): unrelated history is returned only with `include_unrelated=true` (measured 33% → 0% unnecessary context) |
| Session recency | Ordered by the agent's valid time (`last_event_at = max(valid time)`), not by when a batch arrived |
| Explorer: project switcher, filters | Memory-space switcher, 14 new entity types in the type filter, per-type node shapes, no new graph UI. Session/date-range/branch/file filters are the existing time lens, entity picker and the new project history API; there is no dedicated session or date-range filter widget [L] |
| MCP | Stdio adapter in `integrations/mcp/` (separate package, `mcp` 2.2); 17 tools |

Migrations: `3397424d24ed` (`projects`, `project_tokens`), `b2f392141c1b` (`agent_sessions`, `agent_events`,
`git_commits`). Additive only; no existing table altered; no new Neo4j label or index.
