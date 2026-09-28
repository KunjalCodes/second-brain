# Phase 12 — Unified Memory Graph + Coding-Agent Project Memory

**Status:** PLAN, awaiting approval. No implementation has started.
**Checkpoint:** `18c93a9` (Phase 11). The working tree also holds two
uncommitted fixes to the Gemini adapter, found while loading demo data; they
are not part of this plan.
**Written:** 2026-09-28

Evidence labels (same as Phase 11):

| Label | Meaning |
|---|---|
| [V] | Verified by a test or check that ran |
| [I] | Inspected in code (`path:line` at the checkpoint) |
| [M] | Measured locally |
| [A] | Assumption or estimate, to be checked |
| [L] | Limitation |

---

## 1. Executive Summary

Phase 12 makes the memory engine usable in two modes on **one** engine:

1. **Standalone Second Brain** (today): conversations and documents →
   memory graph → retrieval → agent.
2. **Coding-agent memory** (new): Git history and coding-agent sessions →
   project memory graph → retrieval → coding agent (through MCP).

The central design decision (D1): **a project is a memory space**. Each
project gets its own memory-ownership id (`users.id`), owned by the user's
account. ADR 0021 already separates authentication identity from memory
ownership, and every route receives its owner id from one dependency
(`get_current_user_id`, `backend/app/api/deps.py:183-190`, used 53 times in
9 route files [I]). If that dependency returns the project's owner id, then
these all work per project **without being duplicated**:
- temporal logic;
- provenance;
- entity resolution;
- hybrid retrieval;
- review;
- the graph API;
- every Cypher and SQL isolation audit.

The second decision (D2): **code concepts go into the existing model.**
- File, Commit, Bug and similar concepts become new `EntityType` values.
- Their relationships become `RELATES_TO` predicates.
- Every project fact is an ordinary Memory with a Source, a validity window
  and supersede history.

No new Neo4j labels and no second temporal model.

The graph UI becomes **one Graph Explorer** with pluggable domains (memory,
project, entity, history). It adds:
- hover previews;
- fit and reset;
- search-to-focus;
- category filters;
- keyboard access;
- a synchronised timeline;
- a "Why?" panel;
- a restrained visual system based on node categories.

All of it stays inside the Phase 8 server budgets.

What this phase is **not**:
- no second backend;
- no server-side cloning of repositories;
- no full source-code indexing;
- no project-management features;
- no claims about real coding-agent behaviour that has not been run.

---

## 2. Current System State (inspected)

### 2.1 Graph store [I]

**Labels.** Only `Memory`, `Entity` and `Source` exist
(`backend/app/graph/schema.py:24-63`). Schema statements are applied by
`scripts/init_graph.py`, not at startup.

**Relationships:**

| Relationship | Notes |
|---|---|
| `MENTIONS` (Memory→Entity) | |
| `DERIVED_FROM` (Memory→Source) | |
| `RELATES_TO` (Entity→Entity) | Free-text `predicate` matching `^[a-z][a-z0-9_]{0,62}[a-z0-9]$` (`app/models/normalization.py:28`). Properties: `memory_id`, `status`, `valid_from`, `valid_until`, `confidence`. One edge per assertion |
| `SUPERSEDES` | |
| `CONFLICTS_WITH` | |
| `DUPLICATE_OF` | |
| `MERGED_INTO` | |

**Isolation.**
- A `user_id` property is on every node and on most edges.
- Every Cypher constant filters on `$user_id`.
- This is enforced statically by `tests/unit/test_cypher_safety.py`
  (ADR 0019).

**Entity identity.**
- Identity is `(user_id, normalized_name)`, unique (`schema.py:24-33`).
- `normalize_name` only collapses whitespace and lowercases.
  - `auth/session.py` stays `auth/session.py`.
  - `README.md` and `readme.md` collide [V].
- On a MERGE, the first `entity_type` wins
  (`memory_repository.py:41-60`).

### 2.2 Temporal model [I]

- **Two time axes** (bi-temporal):
  - valid time `valid_from`/`valid_until`, half-open;
  - recorded time `created_at`/`updated_at` (`app/models/lifecycle.py`).
- **Statuses:** active, historical, superseded, invalid, conflicting,
  archived, merged.
- **Supersede** (`SUPERSEDE_MEMORIES`, `memory_repository.py:93-111`) closes
  the old memory and its `RELATES_TO` edges in one transaction. Nothing is
  deleted.
- **As-of:** the graph API has `temporal_scope = current | as_of | all`
  (`app/schemas/graph.py:28-39`). It applies the scope on every hop, before
  any LIMIT (`graph_repository.py:70-99`).

### 2.3 Provenance [I]

- A memory links to one or more Sources through `DERIVED_FROM`.
- `Source.reference` is a PostgreSQL message or document id.
- `Source.location` is `chunk:<id>:<span>` for documents.
- `ProvenanceService.resolve_sources` (`app/services/provenance_service.py:24-107`)
  resolves only MESSAGE and DOCUMENT today.

### 2.4 Graph API and budgets [I]

**Routes:**
- `GET /graph/overview`
- `GET /graph/entities/{id}/neighborhood`
- `GET /graph/edges/evidence`
- `GET /graph/memories/{id}/timeline`

All are rate limited under the `graph` class (120/min).

**Budgets** (`app/core/config.py:295-298`):

| Budget | Default | Hard ceiling |
|---|---|---|
| `max_nodes` | 150 | 1000 |
| `max_edges` | 400 | 3000 |
| `max_hops` | 2 | 3 |
| `max_memories_per_entity` | 5 | 25 |

**Budget enforcement:**
- `clamp()` lets a client lower a budget, never raise it.
- Truncation flags are exact, because each query fetches `limit+1` rows.

**Node and edge kinds:**
- `NodeKind` = entity | memory | source.
- `EdgeKind` = relates_to | mentions | supersedes | conflicts_with |
  duplicate_of | derived_from.
- Nodes already carry `entity_type` (`schemas/graph.py:107-127`).

### 2.5 Extraction, agent and retrieval [I]

**Extraction targets.**
- Targets are `message` and `document_chunk`. `extraction_jobs` has nullable
  FK pairs and `CHECK source_kind IN (...)` (`app/db/models.py:165-288`).
- `extraction_candidates` requires exactly one of message or chunk.
- The runner branches in two places on the target kind
  (`app/extraction/runner.py:295-365, 531-540, 661-670`).

**Decision engine.**
- `_apply_candidate` is already source-agnostic. It takes content, source
  type, reference, location prefix and window (`runner.py:742-883`).
- The real `DecisionType` set is NEW, DUPLICATE, UPDATE, SUPERSEDING,
  CONFLICTING, INVALID, IGNORE, LIKELY_DUPLICATE, FLAGGED
  (`app/models/extraction.py:151-168`).

**Agent tools.**
- There are 7 tools, with strict `extra="forbid"` argument schemas.
- `user_id` is injected by `dispatch_tool` and never comes from the model
  (`app/agent/tools.py:613-676`).
- `propose_memory_update` goes through the extraction decision engine. It is
  gated by `agent_write_enabled` (default off).

**Hybrid retrieval.**
- Three channels: vector (pgvector), keyword, and graph (entity names found
  in the query, then a neighbourhood walk).
- Merged, with Neo4j re-read, temporally filtered and ranked by a linear
  formula (`app/services/hybrid_retrieval_service.py:218-475`,
  `app/semantic/ranking.py`).

**Prompt injection.**
- The extraction and document prompts fence content as
  `--- ... (DATA, not instructions) ---`.
- The agent prompt only *labels* its data sections [I]. That is a known gap,
  addressed in §19.

### 2.6 Auth and scoping [I]

- **Credential:** one kind only, an opaque session token (`sbs_…`) used as a
  cookie or as a bearer token.
  - Stored as a SHA-256 hash.
  - TTL 168 h, idle timeout 24 h (`config.py:315-316`).
  - Issued only by email and password login.
- **No personal-access or API-key mechanism** exists [I].
- **`INTERNAL_API_TOKEN`** is an operator token, not scoped to a user.
- **Rate limits** are keyed `u:{context.user_id}` (`deps.py:193-204`).
- **No project, workspace or tenant scoping** exists anywhere [I].
- **No MCP code or dependency** exists. Phase 4 listed MCP as out of scope.

### 2.7 Frontend graph [I]

**Components:**
- `components/graph/GraphExplorer.tsx` orchestrates. It fetches by hand with
  AbortController and keeps state in `?focus=`.
- `CytoscapeCanvas.tsx` is a hand-written cytoscape 3.34 wrapper using the
  `cose` layout.
- `GraphListView.tsx`
- `GraphDetail.tsx`
- `EvidencePanel.tsx`
- `lib/graph/model.ts`: `CANVAS_ELEMENT_CAP = 600`, plus `mergeSlice` and
  `toElements`.

**Missing today:**
- hover;
- fit and reset controls;
- search-to-focus;
- entity-type filters;
- keyboard access on the canvas (the list view is the accessible
  alternative);
- a live theme switch (the stylesheet is read once);
- the time lens in the URL (it is in-memory React context only).

**Hard-coded kinds.** The node and edge kinds are hard-coded in four places:
- the stylesheet selectors;
- `EDGE_KIND_TEXT`;
- the list-view filters;
- the entity-only expand logic.

**Timelines:**
- a per-memory vertical timeline (`components/temporal/MemoryTimeline.tsx`);
- no space-level timeline.

### 2.8 Measured baselines [M]

Measured at `18c93a9` plus the uncommitted Gemini fixes, on Apple M1:
- Backend: **1418 passed** (full suite, 2026-09-28).
- Frontend: 69 unit tests.
- E2E: 29/29.
- Graph conformance: 39/39.

**Graph end-to-end timings:**

| Graph size | Layout | Backend |
|---|---|---|
| ~150 nodes (299 elements) | 431–481 ms | 134–276 ms |
| ~300 nodes (597 elements) | ~1.55 s | 85–279 ms |

- Headless cose: 600 elements take 1.6 s; 1200 elements take 6.4 s.
- The bottleneck is **layout**.

**Gemini free tier:** `gemini-2.5-flash` allows **20 requests per day** on
the current key (measured from a 429 quota id). This caps any LLM-heavy
ingestion.

### 2.9 Deployment limits [I][P]

| Service | Limits |
|---|---|
| AuraDB Free | 200k nodes / 400k relationships, shared by all users and data; pauses after 72 h without activity |
| Neon Free | 0.5 GB |
| Render Free | Sleeps when idle; ephemeral disk |

---

## 3. Product Model

```text
                    PERSISTENT AI MEMORY ENGINE
                              |
                +-------------+-------------+
                |                           |
         STANDALONE MODE             AGENT INTEGRATION
       (web UI, personal space)   (MCP adapter, project spaces)
                |                           |
      Conversations / Docs       Git batches / Session events
                |                           |
                +-------------+-------------+
                              |
              MEMORY ENGINE (unchanged core, per memory space)
      extraction+decisions · entity resolution · temporal · provenance
                              |
                +-------------+-------------+
                |             |             |
             Temporal      Semantic       Graph
          (valid/recorded) (pgvector)    (Neo4j)
                +-------------+-------------+
                              |
                       Hybrid Retrieval
                              |
              Agent tools  ·  Project tools (REST + MCP)
                              |
             Graph Explorer (memory / project / entity / history)
```

**A memory space** is the unit of isolation. It is one `users.id` value,
which is the Neo4j `user_id` property.
- **Personal space:** each account's existing id. Unchanged.
- **Project spaces:** one new id per project, owned by an account.

Standalone mode uses the personal space. Coding-agent mode uses a project
space. The web UI can open either.

---

## 4. Architecture Decision: Project = Memory Space (D1)

### 4.1 Options

| | A. Project = memory space (recommended) | B. `project_id` property on every node/edge/row |
|---|---|---|
| Isolation | Reuses `user_id` isolation. The Cypher and SQL audits apply unchanged | Every Cypher constant and repository needs a second filter; the audits must be extended; one missed filter leaks data |
| Code touched | Auth dependency, project tables, new ingestion paths | Every repository, query, index and service signature |
| Existing data | Untouched; the personal space is byte-identical | Needs a migration or a "null project" rule everywhere |
| Cross-project queries | Not possible without an explicit fan-out (by design) | Easy, but easy to leak by accident |
| Entity resolution | Per space. "Auth" in project A never merges with "Auth" in project B | Needs a project filter in resolution |
| Cost | One `users` row per project | None extra |

**Recommendation: A.** The prompt requires isolation between projects and
tests for it. Option A makes a leak structurally impossible through the
existing, already-audited `$user_id` filter.

The trade-off is that "search across all my projects" is **not offered** in
Phase 12 (D8).

### 4.2 Resolution rules

`get_auth_context` gains three fields:
- `account_id`: the account's personal `users.id`;
- `space_id`: the effective memory owner;
- `project_id`: null in the personal space.

`get_current_user_id` returns `space_id`. The route, service and repository
signatures stay the same.

| Credential | How the space is chosen | Failure |
|---|---|---|
| Session (cookie or bearer), no header | Personal space (today's behaviour) | – |
| Session plus `X-Memory-Space: <project_id>` | The project, if it belongs to this account and is active | **404** `project_not_found`; never 403, so projects cannot be probed |
| Project access token (`sbp_…`, §11) | Always the token's project. Any `X-Memory-Space` header must match | 403 `space_mismatch` |
| `dev_header` mode | Personal space only. Project headers are refused | 400 |

**The server always derives the space id.** A client names a *project*;
the server checks ownership and maps the project to its owner id. No request
can supply an owner id, so ADR 0021's invariant holds. The auth audit test
gains a rule: the space header may only be read by `resolve_space`.

**Rate limits move to `u:{account_id}`.** Today they are keyed on
`context.user_id`. If they followed the space, a user could multiply their
quota by creating projects.

**Project tools** (§10) require a **project** space and refuse the personal
space. A coding agent therefore never reads personal conversations.

---

## 5. Coding Project Memory Model (D2)

### 5.1 Entity types

These are additive `EntityType` values (`app/models/enums.py:62-74`).
Values are stored verbatim, and adding one needs no data migration.

| Concept | `entity_type` | Canonical name, which is the identity key |
|---|---|---|
| Project | `project` (existing) | Project display name |
| Repository | `repository` | `repo:<normalized remote or local id>` |
| Directory | `directory` | `dir:<path>/` |
| File | `file` | `file:<path>` |
| Class / Function | `code_symbol` | `sym:<path>#<symbol>` (only from diff hunk headers, §7.4) |
| Dependency | `dependency` | `dep:<ecosystem>:<name>` |
| Feature | `feature` | LLM- or agent-named, resolved per space |
| Bug | `bug` | LLM- or agent-named |
| Decision | `decision` (existing) | LLM- or agent-named |
| Agent Session | `agent_session` | `session:<client>:<session id>` |
| Commit | `commit` | `commit:<full sha>`; the label shows 7 characters |
| Branch | `branch` | `branch:<name>` |
| Change | `change` | LLM-named change summary |
| Error | `error` | LLM- or agent-named |
| Fix | `fix` | LLM- or agent-named |

**Structural identities** (repository, directory, file, code_symbol,
dependency, agent_session, commit, branch):
- are **deterministic prefixed names**;
- are excluded from fuzzy duplicate flagging, via a new
  `EXACT_IDENTITY_TYPES` set checked in `app/extraction/resolution.py`.

This stops `auth/session.py` and `auth/sessions.py` from ever being proposed
as a merge.

**Paths:**
- are always repository-relative and POSIX;
- are NFC-normalised;
- keep their case in `name` (normalisation lowercases only the identity
  key).

The resulting case-folding collision is [L] and is documented.
Case-sensitive repositories that contain two files differing only by case
are rare. If one is detected, the second path gets a `~case` suffix and a
warning, so the collision is never silent.

### 5.2 Relationships

All relationships are `RELATES_TO` predicates.
- Predicates stay free text under `PREDICATE_PATTERN`.
- A new `PROJECT_PREDICATES` reference set documents the vocabulary.
- Direction follows the prompt.

| Structural (deterministic, §7) | Semantic (extraction or agent, §7.5, §8) |
|---|---|
| project `contains` repository | commit `implemented` feature |
| repository `contains` directory/file | commit `fixed` bug · bug `fixed_by` commit |
| directory `contains` file | commit `introduced` change |
| file `contains` code_symbol | bug `introduced_by` commit (when evidenced) |
| commit `changed` file | bug `discovered_during` agent_session |
| commit `added`/`deleted` file | decision `led_to` change |
| file `renamed_to` file | decision `related_to` feature |
| commit `on_branch` branch | decision `superseded_by` decision |
| commit `follows` commit (parent, first parent only) | agent_session `worked_on` feature |
| repository `depends_on` dependency (manifest diffs only) | agent_session `modified` file · `discovered` bug · `made` decision |

**Inverse pairs are not both written.** For example, `commit fixed bug` and
`bug fixed_by commit`: only the direction the evidence states is stored.
The UI and the tools read both directions. This avoids doubling edges.

**"A superseded_by B" is stored twice, as each layer's normal mechanism:**
- the fact-level history is the existing `SUPERSEDES` edge (B's memory
  supersedes A's);
- the entity-level link is a `superseded_by` predicate between the two
  decision entities.

---

## 6. Temporal and Provenance Rules for Project Facts

**Valid time = when it happened in the project.**
- Commit facts use the commit's **committer date**; the author date is kept
  in the source row.
- Session facts use the event timestamp.

**Recorded time** = ingestion time (`created_at`). This distinguishes "when
it happened" from "when we learned it", which is exactly ADR 0028.

**File lifecycle:**
- The memory "repository R contains file F" is created at the first commit
  that adds F.
- A later deletion creates "commit C deleted file F", which **supersedes**
  the contains-memory. Its `valid_until` becomes C's time.
- A rename supersedes the old contains-memory and creates a new one plus
  `renamed_to`.

"What did the module look like as of March" is then an ordinary `as_of`
query.

**Decision replacement.** A new decision memory lists the old one in
`supersedes`. This uses the existing supersede path, and nothing is
overwritten.

**Bugs:**
- "When introduced" = `valid_from` of the `introduced`/`introduced_by`
  memory, only when there is evidence: a commit message, the agent's
  statement, or a revert/fix reference.
- "When fixed" = `valid_from` of the `fixed` memory.
- The system never infers an introduction commit by blame heuristics in this
  phase [L].

**Provenance.** Every project memory has a Source, using new `SourceType`
values:

| `source_type` | `reference` | `location` |
|---|---|---|
| `git_commit` | PostgreSQL `git_commits.id` | `file:<path>` or `hunk:<path>:<n>` when a specific hunk is cited |
| `coding_session` | `coding_session_events.id` | `event:<seq>` |

`ProvenanceService` gains resolvers for both. They return a commit sha,
message excerpt, author date and changed path, or an event summary.

**Raw event → memory layers.** The prompt's six distinctions map as follows:

| Layer | Where it lives |
|---|---|
| Raw event | PostgreSQL `git_commits` and `coding_session_events` (bounded, never in Neo4j) |
| Extracted memory | `Memory` |
| Project entity | `Entity` |
| Relationship | `RELATES_TO` carrying `memory_id` |
| Decision | `Entity(decision)` plus decision memories |
| Historical state | Memory status and validity, plus `SUPERSEDES` |

---

## 7. Git Ingestion (M2)

### 7.1 Push model (D3)

The server **never clones a repository**. A local collector reads Git and
pushes bounded JSON batches.
- It is `scripts/git_collect.py`, stdlib only, calling `git log`,
  `git show --numstat` and `git diff --unified=0`.
- The MCP adapter (§9) reuses the same module.

Why a push model:

| Reason | Detail |
|---|---|
| No credentials | The server never needs Git credentials |
| No SSRF | The server never fetches a URL |
| No disk | No server disk use; Render's disk is ephemeral |
| User control | The user decides what leaves their machine |
| Coding agents | They already run next to the repository |

### 7.2 API

| Route | Purpose |
|---|---|
| `POST /projects/{project_id}/git/batches` | Up to 50 commits per batch, JSON, max 1 MiB. Accepts a session credential plus project ownership, or a project token with the `ingest` scope. Rate class `ingest` |
| `GET /projects/{project_id}/git/batches/{id}` | Status |
| `GET /projects/{project_id}/git/commits?path=&limit=` | Raw commit rows, bounded, for provenance display |

A batch item contains:
- `sha`, `parents`;
- `author_name` (optional, off by default: D4);
- `committed_at`, `authored_at`;
- `message`, capped at 8 KB;
- `branch`;
- `files[]`, capped at 200 per commit, each with `path`, `old_path`,
  `status` (A/M/D/R), `additions` and `deletions`;
- an optional `hunks[]`, capped at 20 per file and 2 KB each.

The server validates everything again and never trusts the collector's
bounds.

### 7.3 Filters (both collector and server)

**Path excludes** (configurable):
- lockfiles;
- `node_modules/`, `vendor/`, `dist/`, `build/`, `.next/`;
- `*.min.*`, binaries;
- `.env*`, `*.pem`, `*.key`.

**Secret filter:**
- `scripts/secret_scan.py`'s patterns run over messages and hunks.
- A matching hunk is **dropped**, never redacted in place, and the item is
  marked `secret_filtered=true`.
- A matching message is replaced by `[message withheld: possible secret]`.

**Other rules:**
- Merge commits: metadata only, no diff.
- Default history bound: the last **500 commits** of the chosen branch. The
  user can raise it to 2000; beyond that is refused.

### 7.4 Deterministic structural layer (always on, no LLM)

For each new commit (idempotent on `(space_id, sha)`), the server writes
**one** structural memory through `MemoryService.create`. Example: "Commit
a1b2c3d changed auth/session.py, auth/tokens.py (+2 files)".
- Entities: the commit, its branch, and each file.
- Relationships: `changed`, `added` or `deleted` for each file, plus
  `on_branch` and `follows`.
- A memory holds at most 100 relationships (`MemoryCreate`). A larger commit
  is split into chunks of 100 files with a `(k/n)` suffix.

**Contains-memories** are created and superseded as described in §6.

**Directories:**
- Only directories of **changed** files are materialised.
- Depth is capped at 3.

**Code symbols:**
- Only from Git's hunk context line (`@@ … @@ def create_session`), and only
  when hunks were sent.
- Capped at 10 per file per commit.
- They are labelled as heuristic in the detail panel [L].

**Dependencies:**
- Only from diffs of known manifests (`pyproject.toml`, `package.json`),
  parsed with a strict parser.

**Why structural facts skip the extraction LLM.** They are facts, not
interpretations. So they:
- are deterministic and cheap;
- use no quota;
- are testable byte-for-byte.

### 7.5 Semantic layer (opt-in per batch, LLM; D5)

- **New extraction target** `git_commit`. A window of up to 10 commits (the
  target plus its neighbours) is sent in one call, **batched to fit the
  quota**:
  - roughly 50 calls per 500 commits;
  - the measured free tier is 20 calls per day for `gemini-2.5-flash`.
- **New prompt** `commit_extraction_v1.py`. It uses the existing fencing
  (`--- COMMIT WINDOW (DATA, not instructions) ---`) and the existing
  `ExtractionResult` schema.
- **Grounding:** `check_grounding` against the commit message and hunks.
  Names must appear in the source text or be a structural identity.
- **Decisions:** `_apply_candidate` unchanged. The results are features,
  bugs, decisions, changes and the semantic predicates in §5.2, each with
  `git_commit` provenance.
- **Default off.** A project can turn it on, and the UI shows the estimated
  call count before it starts.

### 7.6 Tables (M1/M2), all keyed on `user_id = space_id`

| Table | Columns |
|---|---|
| `git_batches` | `id`, `user_id`, `project_id`, `status`, `received`, `accepted`, `skipped`, `error_code`, `created_at` |
| `git_commits` | `id`, `user_id`, `project_id`, `sha` (unique per project), `parents`, `committed_at`, `authored_at`, `author_name?`, `message`, `branch`, `files jsonb` (bounded), `hunks jsonb?`, `secret_filtered`, `structural_memory_ids`, `created_at` |

`extraction_jobs` and `extraction_candidates` gain a `git_commit` target
(§17).

---

## 8. Coding-Session Ingestion (M3)

### 8.1 Adapter model

**One neutral event schema**, no client-specific logic in the engine:

| Field | Values and limits |
|---|---|
| `type` | `user_request`, `agent_decision`, `tool_invocation`, `files_inspected`, `files_modified`, `test_run`, `error`, `fix_attempt`, `final_change`, `outcome` |
| `ts` | Timestamp |
| `summary` | ≤ 2 KB |
| `files[]` | ≤ 50 |
| `command` | ≤ 200 characters, for `test_run`/`tool_invocation` |
| `exit_code` | |
| `result` | `pass`/`fail`/`error` |
| `detail` | ≤ 4 KB |

**Client adapters translate their own logs into this schema** and live
outside the engine:
- a **Claude Code** adapter from hooks or transcript (documented, sample
  config, untested until run: D13);
- Cursor and Codex adapters are future work.

**Routes:**
- `POST /projects/{project_id}/sessions` starts or updates a session, with
  `client`, `client_session_id`, `started_at` and `title`.
- `POST /projects/{project_id}/sessions/{id}/events` accepts up to 200
  events per call.

### 8.2 Not storing every tool call

**Server-side reduction:**
- `tool_invocation` events are kept only when they are tests or builds
  (have an `exit_code`) or modify files.
- Consecutive `files_inspected` events are collapsed into one.
- A session is capped at 2,000 stored events; the rest are counted and
  dropped.

**Structural layer (deterministic):**
- an AgentSession entity;
- `modified` a file, from `files_modified` and `final_change`;
- `worked_on` a feature, only when named by an event.

**Semantic layer:**
- New extraction target `session_event`, a window of about 20 reduced
  events, with prompt `session_extraction_v1.py`.
- The result: decisions (`made`), bugs (`discovered`), errors, fix
  attempts, and "tried X, reverted" outcomes. This answers "what did the
  agent try before?".

**Tool output is untrusted data.** It is fenced, never followed, and is
subject to the secret filter (§7.3).

**Tables:**

| Table | Columns |
|---|---|
| `coding_sessions` | `id`, `user_id` (= space), `project_id`, `client`, `client_session_id` (unique per project), `title`, `started_at`, `ended_at`, `event_count`, `dropped_count` |
| `coding_session_events` | `id`, `user_id`, `session_id`, `seq`, `type`, `ts`, `summary`, `files`, `command`, `exit_code`, `result`, `detail`, `secret_filtered` |

---

## 9. Integration Layer and MCP (M4)

### 9.1 Shape

```text
Coding agent (Claude Code / Cursor / Codex / any MCP client)
      │  MCP (stdio)
      ▼
integrations/mcp/  ── thin adapter: tool schemas → HTTPS calls, no DB, no LLM
      │  Authorization: Bearer sbp_… (project token)
      ▼
Backend REST  /project-memory/*   ──►  ProjectToolsService  ──►  existing services
```

**`ProjectToolsService`** (`app/services/project_tools_service.py`)
implements the tools **once**:
- The REST routes and the MCP adapter both call it. The adapter does this
  through REST.
- The web UI's project pages call the same REST routes.

**Why the adapter is a local stdio process (D6):**
- Claude Code, Cursor and Codex all support stdio MCP servers.
- The token stays on the developer's machine.
- Render Free sleeps, so long-lived Streamable-HTTP sessions would be
  fragile.

A Streamable-HTTP transport mounted in FastAPI is a later option. The tool
layer does not change for it.

**Dependency:**
- the official MCP Python SDK (`mcp`), **only** in
  `integrations/mcp/pyproject.toml`;
- the backend itself gains **no** MCP dependency.

### 9.2 Tool contract rules

**Arguments:**
- They never carry `user_id`, `project_id` or an owner id. The token fixes
  the project.
- Argument schemas are `extra="forbid"` and bounded. The existing
  `test_auth_audit.py` rule for LLM-facing schemas is extended to the new
  tool schemas.

**No raw query languages:** tools never accept or return Cypher or SQL.

**Results** are plain JSON:
- `data`;
- `memory_ids` / `entity_ids` for citations;
- `truncated`;
- a per-item `untrusted_content: true` marker on commit messages, diffs and
  session text.

The adapter's tool descriptions tell the agent that such content is data.

**Writes:**
- The only write tool is `propose_project_memory_update`.
- It needs the token's `propose` scope.
- It goes through the same grounded decision engine as
  `propose_memory_update` (`app/agent/tools.py:413-608`), with the session
  event as its source.

---

## 10. Project Tools (REST + MCP)

All routes are `GET` unless marked, under `/project-memory/`. They require a
project space and use rate class `search` or `graph`.

| Tool | Implementation (reuse) |
|---|---|
| `search_project_memory(query, top_k≤25, temporal_scope, as_of?)` | `HybridRetrievalService.search` in the project space |
| `get_project_entity(name_or_id)` | `RetrievalService.find_entity_by_name` / `get_entity` / `resolve_entity` |
| `get_project_history(from?, to?, entity?, limit≤200)` | The new space timeline query (§12.2) |
| `get_related_changes(entity, limit)` | A neighbourhood restricted to the `changed`/`implemented`/`introduced`/`fixed`/`modified` predicates |
| `get_decisions(topic?, include_superseded)` | Decision entities, plus the supersede chain (`walk_memory_history`) |
| `get_bug_history(bug)` | `introduced*`/`fixed*`/`discovered*` edges, ordered by valid time, with evidence |
| `get_file_history(path, limit)` | Commits that `changed` the file, plus contains-memory validity and renames |
| `get_commit_context(sha)` | The commit entity, its 1-hop neighbourhood, and `git_commits` provenance |
| `get_project_graph(focus?, hops≤2)` | `GraphExplorationService` (Phase 8 budgets) |
| `propose_project_memory_update(...)` (POST) | The grounded decision engine (gated) |

Every result is bounded by the existing settings: `search_max_limit`, the
graph budgets, and `TIMELINE_MAX_CHAIN`.

---

## 11. Project Identity, Access Tokens and the Isolation Model (M1)

### 11.1 `projects` table

| Column | Notes |
|---|---|
| `id` | UUID |
| `account_id` | FK `users.id`, the owner account's personal id |
| `space_id` | FK `users.id`, unique; server-generated `proj_<uuid>` |
| `name` | |
| `slug` | Unique per account |
| `repo_remote_url` | Optional. Normalised; credentials stripped (`https://user:tok@…` → `https://…`) |
| `repo_root_commit` | Optional. Stable identity of a local repository |
| `local_id` | Optional. Client-chosen label, not trusted for authorisation |
| `status` | active / archived |
| `created_at`, `updated_at` | |

**Constraints:**
- unique `(account_id, repo_root_commit)`;
- unique `(account_id, repo_remote_url)`, excluding nulls.

These stop the same repository being attached twice by accident.

**Creation** (`POST /projects`) creates:
- the project row;
- the `users` row for `space_id`;
- the Project entity in Neo4j (`project contains repository`).

**Deletion:**
- Phase 12 offers **archive** in the UI.
- Permanent deletion is an operator CLI (`scripts/manage_projects.py
  delete`) that removes PostgreSQL rows and Neo4j nodes for `space_id`,
  with a confirmation prompt.

### 11.2 Project access tokens (D7)

**Table `project_tokens`:**

| Column | Notes |
|---|---|
| `id` | |
| `project_id` | |
| `account_id` | |
| `name` | |
| `token_sha256` | |
| `scopes` | ⊆ {`read`, `ingest`, `propose`} |
| `created_at`, `last_used_at` | |
| `expires_at` | Default 30 days, max 90 |
| `revoked_at` | |

**Tokens:**
- Format: `sbp_` plus 43 URL-safe characters, generated like session
  tokens (`app/auth/tokens.py`).
- **Shown once** at creation; only the hash is stored.

**Managed from the web UI:**
- `POST /projects/{id}/tokens` (session only; a token cannot mint tokens);
- `GET /projects/{id}/tokens`;
- `DELETE /projects/{id}/tokens/{token_id}`.

**Checks on every request:**
- the token is not revoked or expired;
- the account is active;
- the project is active.

Account disable and logout-all also revoke the account's project tokens.

**Browser boundary:**
- Project tokens are refused on **cookie** transport.
- They are accepted only as a bearer token.
- They are refused on `/auth/*`, `/projects` CRUD and token routes.

So a leaked token cannot administer anything.

### 11.3 Isolation model

| Boundary | Mechanism | Test |
|---|---|---|
| User ↔ user | Existing `user_id` filters and audits | Existing plus new cross-user probes on every new route and tool |
| Project ↔ project (same user) | Different `space_id`; header/token resolution (§4.2) | Two projects share entity names ("Auth"). Every tool, graph route and search returns only its own space |
| Project ↔ personal space | Project tools refuse the personal space; the personal UI never sends the header | Personal search never returns project memories, and the reverse |
| Token ↔ other project | The token pins the space | A header naming another project gives 403; a path naming another project gives 404 |
| Scope | `read` / `ingest` / `propose` checked per route | A read token cannot ingest or propose |
| Rate limits | Keyed on `account_id` | N projects do not multiply quota |

---

## 12. Unified Graph Backend (M5)

### 12.1 Principles

- There is one `GraphExplorationService`, and the domains are **parameters**
  of the same API, not new services.
- Budgets are unchanged (150 / 400 / 2 hops).
- Every new query goes into `graph_repository.py` and its
  `USER_SCOPED_STATEMENTS`, and passes the projection rules in
  `test_cypher_safety.py`:
  - bounded;
  - read-only;
  - temporal scope applied before any limit.

### 12.2 API changes (all additive)

| Change | Details |
|---|---|
| `GET /graph/overview?root=project` | In a project space, starts from the Project entity instead of "recently updated". Without the parameter the output is unchanged |
| `GET /graph/timeline` | **New.** Parameters: `from`, `to`, `entity_id?`, `limit≤200`, `temporal_scope`. Returns space-level events: memories ordered by `valid_from`, plus "ended" events at `valid_until`. Each event has memory id, label, event kind (`began` / `ended` / `superseded`), entity ids and the source type. Built from existing properties; no new store |
| `GET /graph/why` | **New.** Parameters: `node_kind`, `node_id`, `question` ∈ {`origin`, `cause`, `replacement`, `fix`, `source`}. A deterministic backward walk over a **whitelisted predicate set** per question, ≤3 hops and ≤20 paths, plus `SUPERSEDES` and `DERIVED_FROM`. Every step carries its asserting memory id. When nothing is found it returns `paths: []` and `reason: "no_recorded_evidence"`. **No LLM** |
| `GET /graph/nodes/{kind}/{id}/detail` | **New.** A bounded aggregate for the detail panel: node fields, validity and recorded times, counts per predicate and direction, the 5 newest memories, and sources. Project extras (repository, path, commits, features, bugs, decisions, sessions) come from predicates. It saves the panel 4–6 calls |
| `GET /graph/entities/{id}/neighborhood?cluster=true` | **Opt-in** (D10). When more than 12 neighbours share `(predicate, entity_type)`, they are returned as one `kind: "cluster"` node (count, predicate, type) instead of many nodes. Expanding a cluster uses the existing `predicate[]` + `entity_type[]` filters plus a new `after` cursor. Without `cluster=true` the output is byte-identical |
| `NodeKind` | Gains `cluster`, returned only with `cluster=true` |
| `EntityType` | Gains the §5.1 values; generated types are regenerated |

**Search-to-focus** reuses the existing `/entities/search`; no new route.

**Justification for the changed outputs:** none of the existing requests
changes. New parameters and new routes only.

---

## 13. Unified Graph Explorer UI (M6)

### 13.1 One component, domain adapters

```text
GraphExplorer (layout: toolbar · canvas|list · detail panel · timeline dock)
   ├─ domain: memory   (overview → neighbourhood; today's behaviour)
   ├─ domain: project  (root=Project entity; project categories; timeline on)
   ├─ domain: entity   (focus=<entity id>)
   └─ domain: history  (memory timeline slice; supersede/duplicate/conflict)
```

**`lib/graph/domains.ts`** defines the `GraphDomain` interface:
- `load(entry, scope, space)`;
- `expand(node)`;
- `categories`;
- `detailSections`;
- `timeline?`.

The four domains are small objects. Their fetch logic moves out of
`GraphExplorer.tsx`.

**`lib/graph/model.ts`** becomes kind-agnostic:
- node and edge kinds come from the schema, and
- visual semantics come from a category registry.

This removes the four hard-coded places listed in §2.7.

**Data fetching** moves to TanStack Query (keys include `space` and
`scope`), replacing the hand-written AbortController. Query keys are
already defined but unused (`qk.graphOverview`).

### 13.2 Interactions (requirements → design)

| Requirement | Design |
|---|---|
| Pan / zoom | Cytoscape plus toolbar buttons (zoom in/out) |
| Node hover | An HTML hover card (text only, inert) after 150 ms: type, label, status, validity, degree. Hidden on touch devices |
| Edge hover | Card: `A —predicate→ B`, status, assertion count, validity |
| Selection | Click, tap, Enter. Persistent detail panel (§16) |
| Readable labels | Labels shown at zoom ≥ 0.7 or for selected/hovered/neighbouring nodes. Ellipsis at 32 characters. Full text in the card and panel |
| Relationship labels | Predicate shown on hover/select, and on all edges at zoom ≥ 1.2 |
| Node type visualisation | Category shape plus a restrained colour ramp (§14), plus a legend |
| Temporal filtering / "as of" / current vs historical | The time lens moves into the URL (`?as_of=`, `?scope=`). This fixes a Phase 10 limitation and makes views shareable |
| Search-to-focus | Toolbar search → `/entities/search` → choose a result: focus if loaded, otherwise load its neighbourhood |
| Click relationship → evidence | Existing `EvidencePanel`, reused |
| Expansion / neighbourhood | Existing expand plus cluster expand. **Existing positions are locked** during incremental layout (new nodes only), so the view stays stable |
| Fit / reset | Toolbar buttons. Reset re-runs the layout |
| Hide/show categories | Legend toggles. Pure client-side visibility; the budget is unchanged |
| Filtering | Predicate and type filters (server-side through existing parameters) |
| Truncation indicator | Existing banners, plus a cluster count |
| Loading / empty / error | Existing states, per domain wording |

**Keyboard and touch** (hover is never the only way):
- the canvas container is focusable;
- Tab moves between toolbar, canvas and panel;
- arrow keys move the selection to the nearest neighbour in that direction;
- Enter selects, `+`/`-` zoom, `0` fits, `/` focuses search, Esc clears;
- touch: tap selects, long-press expands, two-finger pinch zooms;
- the list view stays the full screen-reader alternative, and a
  `role="status"` region announces the selection.

**Live theme:** a `matchMedia('(prefers-color-scheme: dark)')` listener
restyles the canvas.

### 13.3 Layout

**Default layouts:**

| Domain | Layout |
|---|---|
| memory, entity | `cose`, as today |
| project | `breadthfirst` from the Project root. The project graph is tree-like (contains/changed), so this is cheaper and more readable |
| history | A left-to-right time-ordered preset: x = `valid_from` |

**`fcose`** (the `cytoscape-fcose` extension) is **measured first** (D9)
against cose at 150, 300 and 600 elements. It is adopted only if it is
meaningfully faster. That would be a new dependency, so it needs approval.

The layout runs with `animate: false`. For more than 300 elements, the
toolbar shows "Large view: layout ~N s", using the measured estimate.

---

## 14. Visual Design System (M6)

**Palette:**
- a restrained dark-blue palette built from the existing tokens
  (`app/globals.css`);
- no per-type colours.

**Categories** carry the meaning. There are **5 categories**, each with one
hue step and one shape family:

| Category | Types | Shape | Colour (dark theme) |
|---|---|---|---|
| Knowledge | memory, concept, person, company, technology, other | Ellipse / round-rectangle (today) | Neutral slate / accent-blue outline |
| Structure | project, repository, directory, file, code_symbol, dependency | Rectangle; project = larger, bold border | Steel blue |
| Change | commit, branch, change | Small diamond / chevron | Muted cyan |
| Work | feature, bug, decision, error, fix | Hexagon (feature/decision), octagon (bug/error), tag (fix) | Indigo; **bug/error use the existing warn token only for the border** |
| Agent | agent_session | Rounded tag | Soft violet |

**State:**
- Encoded by **stroke and opacity**, consistently across all types:
  - superseded/historical: 60% opacity with a dashed border;
  - conflicting: warn border;
  - merged: double border;
  - not yet valid: dotted border.
- Status never uses colour alone. The hover card and panel say it in words.

**Edges:**
- a direction arrow;
- width from assertion count, as today;
- historical edges dashed, as today;
- confidence below 0.5 drawn thinner and with a "low confidence" label in
  the card.

**Density rules:**
- Categories collapse into clusters (§12.2).
- Labels appear only by zoom level.
- The canvas cap stays at 600 elements.
- Above that, expansion is disabled with the existing notice.

**Contrast:** all colour pairs checked against WCAG AA in both themes, with
axe in E2E.

---

## 15. Project Graph View and Timeline (M7, M8)

### 15.1 Project routes

| Route | Contents |
|---|---|
| `/projects` | List of projects, with a create button |
| `/projects/[id]` | Overview: counts, latest events, ingestion status |
| `/projects/[id]/graph` | Graph Explorer, project domain |
| `/projects/[id]/timeline` | Full timeline |
| `/projects/[id]/settings` | Tokens, ingestion settings, archive |

**The existing pages work per project.** Memories, entities, search, review
and documents all take a `space` from the route:
- under `/projects/[id]/…`, `apiRequest` sends `X-Memory-Space`;
- outside it, it never does.

**Entry points:**
- you can start from a Project, Feature, File, Commit, Bug, Decision or
  Entity (`?focus=`);
- search-to-focus finds any of them.

### 15.2 Timeline + graph

- **The timeline dock** is a horizontal band under the canvas, plus the full
  page. It reads `GET /graph/timeline`.
- **Events are grouped by day.** Each shows kind, label and category chip.
- **Selecting an event:**
  - sets `?event=<memory_id>`;
  - focuses the graph on the event's entities (loading their neighbourhood
    if needed);
  - highlights the asserted edges.
- **Selecting a node** filters the timeline to `entity_id=<node>`, which is
  its history.
- **The time lens stays in sync:** "view the graph as of this event" sets
  `?as_of=` to the event time.
- **No separate timeline store.** Events come from Memory validity and
  supersede data.

---

## 16. Detail Panel and "Why?" Exploration (M6, M8)

### 16.1 Detail panel

The panel reads `GET /graph/nodes/{kind}/{id}/detail`. It shows:
- node type, name, and content or description;
- current status;
- created (recorded) time and valid time, labelled as in ADR 0028;
- related entities and relationships, grouped by predicate with counts;
- sources and provenance, reusing `ProvenancePanel`;
- history, reusing the memory timeline;
- superseded or replaced information.

**Project node extras:**
- repository;
- file path;
- commit history (from `changed`);
- related features, bugs and decisions;
- agent sessions.

Everything shown comes from stored rows. A missing section says "None
recorded", never a guess.

### 16.2 "Why?"

- **Buttons** on the panel map to `GET /graph/why` questions:
  - "Why does this exist?" → `origin`;
  - "What caused this?" → `cause`;
  - "What replaced this?" → `replacement`;
  - "What fixed this?" → `fix`;
  - "Where did this come from?" → `source`.
- **Results** are rendered as paths. Each step shows the predicate, its
  asserting memory, and a link to evidence.
- **No recorded evidence** shows exactly that. There is no generated
  explanation.
- **Optional prose (D14):** an LLM may summarise the returned paths only if
  approved later. Any summary must cite path memory ids, or it is withheld.
  This phase defaults to **no LLM** here.

---

## 17. Schema Changes and Migration Strategy

### 17.1 PostgreSQL

The new revision is `<rev>_phase12_projects.py`: linear, one head, down
revision `320f6d898a92`.

| Change | Kind |
|---|---|
| `projects`, `project_tokens`, `git_batches`, `git_commits`, `coding_sessions`, `coding_session_events` | New tables with `user_id`/`account_id` FKs to `users.id` (CASCADE) and `(user_id, …)` indexes |
| `extraction_jobs` | New nullable FKs `target_commit_id` → `git_commits`, `target_session_event_id` → `coding_session_events`, plus unique `(target_commit_id, extractor_version)` and `(target_session_event_id, extractor_version)` |
| `extraction_jobs` CHECKs | `source_kind IN ('message','document_chunk','git_commit','session_event')`; the consistency CHECK is rewritten with one branch per kind |
| `extraction_candidates` | New nullable `source_commit_id`, `source_session_event_id`; the "exactly one source" CHECK is rewritten for 4 sources |

**Migration properties:**
- **Additive only.** No existing row changes, no backfill. The CHECK
  rewrites are a DROP and CREATE of the constraint within the same
  transaction, and existing rows satisfy the new CHECK by construction.
- **Downgrade:** destructive and local-only, per DEPLOYMENT.md §9. A new row
  goes into that table.
- **Tests:**
  - `test_migrations.py` covers a fresh database and an existing database
    at the Phase 11 head that holds data;
  - `test_migrations_static.py` requires a single head.

### 17.2 Neo4j

- **No new labels or relationship types.** The new `EntityType` values and
  predicates are data.
- **Optional index**, only if measurements show a need:
  `RELATES_TO(user_id, predicate)` for predicate-filtered project walks.
  Added through `app/graph/schema.py` and `scripts/init_graph.py`.
- **Cypher 5 compatibility check** (AuraDB default) for every new query:
  there is a CI job that runs the conformance tests with `CYPHER 5`
  prefixed.

### 17.3 Existing data

Personal spaces are untouched. Existing entities keep their types.

---

## 18. Files to Create / Change

### 18.1 Backend: create

| Area | Files |
|---|---|
| Models and schemas | `app/models/project.py`, `app/schemas/project.py`, `app/schemas/project_tools.py`, `app/schemas/git.py`, `app/schemas/session.py` |
| Repositories | `project_repository.py`, `project_token_repository.py` (auth-style exemption, like `auth_repository.py`), `git_repository.py`, `coding_session_repository.py` |
| Services | `app/services/project_service.py`, `app/services/memory_space.py` (resolve_space), `app/services/project_tools_service.py` |
| Git | `app/projects/git/validate.py`, `filters.py` (path and secret), `structural.py` (deterministic memories) |
| Sessions | `app/projects/sessions/reduce.py`, `structural.py` |
| Prompts | `app/llm/prompts/commit_extraction_v1.py`, `app/llm/prompts/session_extraction_v1.py` |
| Routes | `projects.py` (CRUD, tokens), `project_ingest.py` (git batches, sessions), `project_memory.py` (tools) |
| Migration | `migrations/versions/<rev>_phase12_projects.py` |
| Eval | `eval/project_runner.py`, `eval/datasets/project_v1.jsonl`, `eval/fixtures/project_repo.py` (builds a synthetic Git repo) |
| Tests | See §22 |

### 18.2 Backend: change

| File | Change |
|---|---|
| `app/api/deps.py` | `AuthContext` gains `account_id`, `space_id`, `project_id`; `resolve_space`; project-token scopes; rate limits keyed on account |
| `app/services/auth_service.py` | Authenticate `sbp_` tokens; revoke on disable and logout-all |
| `app/models/enums.py` | `EntityType` + §5.1; `SourceType` + `git_commit`, `coding_session`; `PROJECT_PREDICATES` |
| `app/models/extraction.py` | `ExtractionSourceKind` + `git_commit`, `session_event` |
| `app/db/models.py` | New tables; job and candidate columns |
| `app/extraction/runner.py` | **Refactor the two-way target branching into a small `TARGETS` mapping** (window loader, target text, source type, location prefix, completion/failure hooks). With four kinds this is justified; `_apply_candidate` is unchanged |
| `app/extraction/resolution.py` | `EXACT_IDENTITY_TYPES` are excluded from fuzzy flagging |
| `app/repositories/extraction_repository.py`, `extraction_queue.py` | Job creation per new target; target failure marking |
| `app/services/provenance_service.py` | Resolvers for the new source types |
| `app/api/routes/graph.py`, `app/services/graph_exploration_service.py`, `app/repositories/graph_repository.py`, `app/schemas/graph.py` | Timeline, why, detail, cluster, `root=project` |
| `app/agent/prompts.py` | Fence the data sections (§19) |
| `app/core/config.py` | Git, session, token and timeline limits |
| `app/main.py` | Include the new routers |
| Tests | `test_sql_safety.py` (exemptions), `test_cypher_safety.py` (AUDITED_MODULES), `test_auth_audit.py` (space header rule; token scopes; tool schemas), `test_route_contract_inventory.py` |

### 18.3 New top level

| Path | Contents |
|---|---|
| `integrations/mcp/` | `pyproject.toml` (depends on `mcp`, `httpx`), `second_brain_mcp/server.py`, `tools.py` (schemas → REST), `README.md` (Claude Code / Cursor / Codex config snippets) |
| `integrations/claude_code/` | Hook scripts and a sample `settings.json` snippet that turn Claude Code hooks into session events |
| `scripts/git_collect.py` | Collector, shared with the MCP adapter |
| `scripts/manage_projects.py` | Operator delete, list |

### 18.4 Frontend: create

| Area | Files |
|---|---|
| Graph logic | `lib/graph/domains.ts`, `lib/graph/categories.ts`, `lib/graph/styles.ts`, `lib/graph/keyboard.ts` |
| Space | `lib/space/space.tsx` (space context; `apiRequest` header) |
| Graph components | `GraphToolbar.tsx`, `GraphLegend.tsx`, `GraphHoverCard.tsx`, `NodeDetailPanel.tsx`, `WhyPanel.tsx`, `GraphTimeline.tsx` |
| Project components | `components/projects/*` (list, create form, token manager with show-once) |
| Routes | `app/(app)/projects/page.tsx`, `[id]/page.tsx`, `[id]/graph/page.tsx`, `[id]/timeline/page.tsx`, `[id]/settings/page.tsx` |

### 18.5 Frontend: change

- `GraphExplorer.tsx` (becomes domain-driven)
- `CytoscapeCanvas.tsx` (hover, keyboard, theme listener, layouts, locked
  positions)
- `lib/graph/model.ts` (kind-agnostic, clusters)
- `GraphListView.tsx`, `GraphDetail.tsx` (folded into `NodeDetailPanel`)
- `EvidencePanel.tsx`
- `lib/time/lens.tsx` (URL sync)
- `AppShell.tsx` (Projects nav)
- `lib/api/endpoints.ts`, `queryKeys.ts`, `types.ts`, `schema.d.ts`
  (regenerated)
- `e2e/*`, `tests/unit/*`

The `frontend/AGENTS.md` rule applies: read `node_modules/next/dist/docs/`
before writing Next.js code.

### 18.6 Docs

| Doc | Change |
|---|---|
| ADR 0033 | A project is a memory space |
| ADR 0034 | Code concepts live in the existing entity/predicate model |
| ADR 0035 | Push-based Git ingestion with a structural and a semantic layer |
| ADR 0036 | MCP adapter and project access tokens |
| ADR 0037 | One Graph Explorer with domains |
| `docs/API.md` | New routes |
| `docs/DEPLOYMENT.md` | Migration row; AuraDB budget |
| `README.md` | Phase 12 roadmap and status |

---

## 19. Security

**Scope and credentials:**
- **Scope** is always server-derived (§4.2). Unknown or foreign projects
  return 404.
- **Project tokens:**
  - hashed and shown once;
  - scoped, expiring and revocable;
  - bearer only;
  - refused on admin routes;
  - revoked on account disable;
  - never logged. The redaction test is extended to `sbp_` tokens.

**Untrusted content.** These are all data:
- commit messages;
- diffs;
- file paths;
- session summaries;
- tool output.

Applied as follows:
- **Extraction prompts** use the existing fences. A test puts an injection
  string in a commit message and a hunk, and checks that it is stored inert
  and never changes a decision beyond the normal schema.
- **The agent prompt** gains the same fences around retrieved data. This
  closes the gap noted in §2.5.
- **Tool results** carry `untrusted_content`. The frontend renders them
  inert (ADR 0029).
- **Delimiter spoofing:** fence strings inside content are neutralised,
  replacing `---` runs at line start. There is a test.

**Secrets in Git.**
- The collector and the server both filter (§7.3).
- Default ingestion is metadata plus diffstat. **Hunks are opt-in** (D4).
- No hunk text is ever sent to the LLM unless semantic mode *and* hunks are
  on.

**Data minimisation:**
- `author_name` is off by default; emails are never collected.
- Remote URLs have credentials stripped.

**Resource limits:**
- batch, commit, file, hunk, event and session caps (§7–8);
- body-size limit;
- rate class `ingest` for batches.

**SSRF / clone:** not applicable, because nothing is fetched server-side.

**Supply chain:** the `mcp` SDK is confined to the adapter package, and
pip-audit covers it in CI.

---

## 20. Performance and Graph Rendering Limits (M10)

### 20.1 Server

- Budgets are unchanged: 150 nodes, 400 edges, 2 hops by default.
- **Timeline:** ≤ 200 events per call.
- **Why:** ≤ 3 hops and ≤ 20 paths.
- **Detail:** ≤ 5 memories and per-predicate counts only.
- **Clusters:** more than 12 siblings of the same `(predicate, type)`
  become one cluster node. This keeps hub files and commits from flooding a
  slice.
- **Nothing streams unbounded graphs.** A 10k-node, 200k-edge project can
  only be seen through bounded neighbourhoods, clusters and progressive
  expansion.

### 20.2 Browser

- The canvas cap stays at **600 elements**.
- **Default first view target: ≤ 300 elements** (measured ~1.5 s cose at
  600).
- The project domain uses `breadthfirst` or preset layouts.
- Positions are locked during incremental expansion.
- `fcose` is adopted only if measured (D9).

### 20.3 AuraDB budget [A]

This is an estimate to verify in M10. A 500-commit project with an average
of 5 files per commit, structural only, comes to about:

| Item | Count |
|---|---|
| Memories | ~500 structural + ~800 contains = ~1.3k |
| Sources | ~500 |
| Entities | ~500 commits + ~1.2k files/dirs = ~1.7k |
| **Nodes total** | **≈3.5k** |
| Relationships | MENTIONS ~7k + RELATES_TO ~4k + DERIVED_FROM ~1.3k = **≈12k** |

Semantic mode adds perhaps 30–50%. AuraDB Free (200k nodes / 400k
relationships, shared) therefore holds only about 20–30 such projects
alongside existing data.

**Mitigations:**
- The ingestion endpoint refuses a batch that would push the instance past a
  configurable node budget (`graph_node_budget`, counted cheaply per space).
- Readiness metrics report graph size.

### 20.4 Measurements

Measured by extending `e2e/perf.spec.ts` and `eval/load_test.py`:
- API latency p50/p95 for overview, neighbourhood (cluster on/off),
  timeline, why and detail;
- **payload bytes**;
- node and edge counts;
- Cytoscape layout and paint time at 50, 150, 300 and 600 elements;
- interaction latency: hover-card show, select → panel, timeline → focus;
- ingestion throughput: commits per minute, structural;
- batch latency.

All results are labelled local.

---

## 21. Backward Compatibility

- **Personal space, no header:** every existing route returns the same
  output.
- **Evidence the outputs are unchanged:**
  - graph conformance 39/39;
  - the retrieval runner byte-identical to `18c93a9`;
  - the document and agent evaluations unchanged;
  - the E2E suite unchanged.
- **Graph API:** new parameters are opt-in. The defaults are unchanged
  (§12.2).
- **Enum additions** (`EntityType`, `SourceType`, `NodeKind`) are additive.
- **Frontend exhaustiveness.** The frontend handles unknown kinds and types
  with a generic style, so an older frontend against the new backend
  degrades gracefully. Drill 11 covers this.
- **The only intentional behaviour change:** rate limits keyed on account
  instead of space. They are identical for personal-space users, because
  the account id equals the space id.
- **The full suite runs before and after each milestone.** The before
  baseline is §2.8: 1418 / 69 / 29 / 39.

---

## 22. Test Strategy

**Deterministic by default:**
- fake LLM with fixture outputs;
- fake embeddings;
- a synthetic Git repository built in a temporary directory by
  `eval/fixtures/project_repo.py`.

**Backend unit tests:**
- project-token format, hashing and scopes;
- `resolve_space` rules (every row of §4.2);
- path normalisation and the case-collision rule;
- secret and path filters;
- batch validation bounds;
- the structural memory builder (golden output);
- session reduction;
- tool argument schemas (`extra="forbid"`, no ownership fields);
- `EXACT_IDENTITY_TYPES` excluded from fuzzy flags;
- fence neutralisation.

**Backend integration tests:**
- project creation → space row + Project entity;
- Git batch ingestion is idempotent (the same sha twice gives one memory);
- file/commit relationships;
- rename and delete supersede, with as-of correctness;
- semantic extraction with fixtures: bug introduced/fixed, decision A
  superseded by B, feature implemented;
- session ingestion → decisions, bugs, "tried and reverted";
- provenance resolution for `git_commit` and `coding_session`;
- timeline, why and detail endpoints;
- cluster on/off equivalence (off is byte-identical);
- truncation flags;
- AuraDB-budget refusal.

**Isolation** (each new route and tool):
- user A vs user B;
- project A vs project B, with the same entity names;
- project vs personal space;
- token for A used against B;
- scope violations.

**MCP:**
- the adapter's tool schemas match the REST contract (a schema-snapshot
  test);
- adapter → REST with a fake HTTP transport;
- authorisation: no token, a revoked token, the wrong scope;
- an **opt-in** smoke test against a running backend with the MCP inspector.

**A real Claude Code / Cursor / Codex run is marked UNTESTED** until it is
actually performed and recorded.

**Frontend unit tests:**
- domain adapters;
- the category registry (every `EntityType` maps to a category);
- keyboard navigation;
- the space header only under `/projects/*`;
- timeline grouping and sync reducer;
- cluster rendering;
- `NodeDetailPanel` sections and "None recorded".

**E2E tests:**
- project graph: create project, seed through the API, explore;
- hover card;
- select → panel;
- edge → evidence;
- search-to-focus;
- category toggle;
- fit/reset;
- timeline → focus, node → history;
- as-of in the URL;
- Why paths and the "no recorded evidence" state;
- keyboard-only walkthrough;
- phone width (list);
- axe on the new pages;
- cross-project isolation in the UI.

**Contract and audit tests:**
- the inventory test covers every new route;
- the Cypher and SQL audits pass with the new modules registered;
- the auth audit covers the space header and tokens.

---

## 23. Evaluation (M10)

**New deterministic harness** `eval/project_runner.py` with dataset
`project_v1.jsonl`, following the `graph_api_runner.py` pattern:
- seed and cleanup;
- exact-match scoring;
- a double run with a byte-identical check;
- persisted with `harness="project"`.

**Task categories:**
- file history;
- commit context;
- bug introduced/fixed times;
- decision replacement;
- feature → files;
- module evolution as of dates;
- why-origin, why-fix;
- session "tried before";
- isolation probes (other project, other user);
- truncation.

**Baselines,** computed on the same synthetic repository and never
fabricated:
1. a **raw `git log` keyword baseline** (grep over messages and paths);
2. **memory graph structural only**;
3. **structural + semantic** (fixture LLM).

Reported as precision/recall per category. Differences are only claimed if
measured.

**Real-LLM variants** are opt-in through `eval/opt_in.py`. They are
reported only if run, and they are quota-bound (20 requests/day on the
current key).

---

## 24. Deployment Validation (M11)

- Local staging dry run (`APP_ENV=staging`) with the new migration:
  - readiness returns `migrations=ok`;
  - project tokens work over bearer;
  - the MCP adapter connects to the local staging URL.
- **Cypher 5 compatibility** of every new query, for AuraDB.
- `render.yaml` / DEPLOYMENT.md:
  - no new services;
  - new environment limits;
  - AuraDB budget guidance;
  - the MCP adapter is run by the user, not on Render.
- **Failure drills added:**
  - a Git batch interrupted mid-way (resume is idempotent);
  - a revoked token mid-session;
  - AuraDB budget reached;
  - an oversize batch.
- **No real deployment** unless the external accounts are supplied. That is
  the same condition as Phase 11 M12.

---

## 25. Milestones

Each milestone ends with the full backend suite, frontend lint, typecheck
and unit tests, and E2E where affected, and a short status note appended to
§30 (Implementation Status, added as work lands).

| # | Milestone | Deliverables | Exit criteria |
|---|---|---|---|
| **M0** | Architecture | This plan; ADRs 0033–0037; decisions D1–D14 approved | Approval |
| **M1** | Project model | Migration (projects, tokens); `resolve_space`; project CRUD + tokens API; rate limits on account; Project entity creation; auth audit updates | Isolation matrix (§11.3) green; existing suite green |
| **M2** | Git ingestion | Collector; batch API; filters; structural layer; `git_commit` target + prompt (semantic, opt-in); provenance resolver | Idempotency, rename/delete supersede, as-of, injection and secret tests green |
| **M3** | Session ingestion | Session API; reduction; structural layer; `session_event` target + prompt; Claude Code hook adapter (documented, marked untested) | Session tests green |
| **M4** | MCP integration | `ProjectToolsService`; `/project-memory/*`; `integrations/mcp` adapter; scopes | Tool contract, authorisation and isolation tests green; inspector smoke test opt-in |
| **M5** | Unified graph backend | Timeline, why, detail, clusters, `root=project`; Cypher audit; Cypher 5 check | Conformance 39/39 unchanged; new endpoint tests green |
| **M6** | Unified graph UI | Domains, category registry, toolbar, hover card, keyboard, legend toggles, detail panel, Why panel, URL time lens, theme listener | Unit and E2E interaction tests; axe clean |
| **M7** | Timeline integration | Timeline dock and page; event ↔ graph sync | Sync tests |
| **M8** | Project graph UX | `/projects/*` pages; breadthfirst layout; project entry points; token manager | E2E project flow |
| **M9** | Testing | Gap-fill across §22; mutation checks on isolation fixes | All suites green, before/after table |
| **M10** | Evaluation + performance | `project_runner` + baselines; §20.4 measurements; fcose decision | Report with measured numbers |
| **M11** | Deployment validation | §24 | Dry run passes; docs updated |

**Commits:**
- one per milestone group: M1; M2–M3; M4; M5; M6–M8; M9–M11;
- the plan and ADRs in their own architecture commit first.

---

## 26. Explicit Non-Goals

- Project management (tasks, boards, assignments, estimates).
- Full source-code indexing or ASTs. Code symbols come only from hunk
  headers.
- Server-side cloning, webhooks, or GitHub/GitLab API integrations.
- Cross-project or cross-user search.
- A second backend, a second temporal model, new Neo4j labels, or a graph
  database other than Neo4j.
- Hosting the MCP adapter on Render.
- Claims about real coding-agent performance that were not run.
- Billing, teams or RBAC. A project has exactly one owner account.

---

## 27. Decisions Requiring Approval

| # | Decision | Recommendation |
|---|---|---|
| D1 | Project scope model | **Project = memory space** (a separate `users.id` per project) |
| D2 | Code concepts | **New `EntityType` values + `RELATES_TO` predicates**; no new labels |
| D3 | Git source | **Push from a local collector**; no server-side clone |
| D4 | Diff content | **Metadata + diffstat by default; hunks opt-in**, bounded and secret-filtered; `author_name` off |
| D5 | LLM use for Git | **Structural always (no LLM); semantic opt-in per project**, batched ~10 commits per call |
| D6 | MCP transport | **Local stdio adapter** calling REST; `mcp` SDK only in `integrations/mcp` |
| D7 | Agent credentials | **Project access tokens** (`sbp_`, scopes read/ingest/propose, 30-day default) |
| D8 | Cross-project queries | **None in Phase 12** |
| D9 | Layout engine | Measure `fcose`; adopt only with evidence (new dependency) |
| D10 | Dense graphs | **Server-side opt-in cluster nodes** (`cluster=true`) |
| D11 | Time lens | **Move into the URL** (`?as_of=`, `?scope=`) for all graph pages |
| D12 | Agent writes | `propose` scope off by default per token; the grounded engine is unchanged |
| D13 | Claude Code session adapter | Ship hook scripts + docs, **marked untested** until a real run is recorded |
| D14 | "Why?" prose | **No LLM summary** in Phase 12; deterministic paths only |

---

## 28. Risks and Open Questions

| Risk | Mitigation |
|---|---|
| AuraDB Free capacity (§20.3) | Per-space counts, an ingestion refusal budget, default 500 commits |
| LLM quota (20/day measured) makes semantic mode slow | Batched windows; structural layer useful alone; deferral already non-destructive (Phase 11) |
| Case-folded path collisions | `~case` suffix plus a warning; documented [L] |
| History rewrites (force-push): ingested commits that are no longer reachable | Kept as recorded history; a later "unreachable" marker is future work [L] |
| Semantic extraction invents bug/decision links | Grounding against message and hunks; LLM proposes, app decides (ADR 0007); review queue for conflicts |
| `entity_type` first-wins merge (e.g. an LLM names a feature "auth/session.py") | Structural identities use prefixed names (`file:`), so free-text names cannot collide with them |
| Runner refactor regressions | The mapping refactor lands alone first, with the full suite and evaluations byte-compared, before new targets are added |
| UI scope creep | The domain interface is fixed in M6; new domains are data, not new components |
| MCP SDK churn | Isolated in the adapter; the REST contract is the stable interface |

---

## 29. Acceptance Criteria

1. **Standalone unchanged.** All §21 evidence holds: suites green,
   conformance 39/39, retrieval byte-identical, E2E 29+ green.
2. **Isolation proven.** Every row of §11.3 has a passing test, including
   project A vs B with identical entity names, and token misuse.
3. **Project memory works deterministically.** On the synthetic
   repository, `project_runner` answers file history, commit context, bug
   introduced/fixed, decision replacement, module evolution as of a date,
   and why-origin. Results are reported against the `git log` baseline,
   with measured numbers.
4. **Provenance everywhere.** Every project memory resolves to a commit or
   a session event. A test asserts no project memory lacks a source.
5. **MCP adapter** passes contract and authorisation tests. A real-agent
   run is either recorded or explicitly marked UNTESTED.
6. **One Graph Explorer** serves the memory, project, entity and history
   domains. Every §13.2 interaction has a test. Keyboard-only and
   phone-width flows pass. Axe is clean.
7. **Timeline ↔ graph sync** works both ways, from existing temporal data
   only.
8. **Why?** returns only evidence-backed paths, or "no recorded evidence".
9. **Budgets hold.** No response exceeds the Phase 8 budgets. §20.4
   measurements are reported, labelled local.
10. **Security.** Token, secret-filter and injection tests pass. The
    redaction test covers `sbp_` tokens.
11. **Docs.** ADRs 0033–0037, API.md, DEPLOYMENT.md and README updated.
    §30 Implementation Status appended.
