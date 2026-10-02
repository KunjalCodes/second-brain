# Phase 16 — Project memory (as built)

Labels: [V] verified, [M] measured, [I] inspected, [A] assumption, [L] limitation.

## 1. Project = memory space [V][I]

A project is an isolated memory space owned by one account (ADR 0034). It owns a dedicated `users` row
(`projects.space_id`) that every existing repository and audited Cypher statement already filters by, so
isolation between projects holds **by construction**, with no repository change. The server resolves a request to
a space only after an ownership check (account + `X-Memory-Space`) or a token check (`sbp_`); a project the caller
does not own is a 404 identical to one that does not exist.

**Limitation (accepted, documented):** personal memory and project memory are separate spaces. There is no
cross-space search. A future option would be a read-only federation layer; it was deliberately not built [L].

## 2. Identity (M3) [V]

`project_id` is a server-generated UUID. Repository identity is a salted SHA-256 of the **normalised remote**
(`host/owner/repo`, lower-case, credentials/scheme/port/`.git` removed; https/ssh/scp spellings collapse), or, with no
remote, of the sorted root-commit hashes. Registration is idempotent per `(account, identity)` and race-safe (a
unique constraint decides; 6 concurrent registrations create one project). **Filesystem paths are refused** and
never stored or exposed. A remote beats root commits when both exist; a repository whose remote changes becomes a
new project [L]. Each ingestion batch echoes the repository identity; a mismatch is a 409, so repository B cannot be
pushed into project A by mistake (a safety check, not authentication).

## 3. Tokens [V]

`sbp_` + 43 chars, SHA-256 at rest, shown once, scoped (`read|ingest|propose`), always expiring (default 90 days,
max 365), revocable, bound to exactly one project, limited per project. Never returned by any query, never
logged (tested against logs and every response). Minting, listing and revoking need an account session.

## 4. Graph model [V][I]

No new labels or relationship types. New **entity types**: `repository, branch, directory, file, code_symbol,
dependency, feature, bug, agent_session, commit, change, error, fix, test_run` (plus the existing `project`,
`decision`). New **source types**: `git_commit`, `agent_event`. Predicates stay free text; the project ones
(`contains, depends_on, modified, inspected, made, encountered, fixed_by, partially_fixed_by, attempted_fix,
superseded_by, manifested_as, committed, changed, defines, child_of, on_branch, points_at, …`) are registered as
**multi-valued**, so accumulating history is never read as a contradiction.

Exact identity: structural entities have deterministic names (`file:<path>`, `commit:<sha12>`, `session:<key>`,
`dir:<path>`, `symbol:<path>::<name>`, `bug:<title>` …) and are excluded from fuzzy duplicate flagging
(`EXACT_IDENTITY_TYPES`), which also spares embedding quota. `auth/session.py` and `auth_session.py` stay distinct [V].
**Only evidenced relationships are persisted**: an event emits an edge only if it states it.

## 5. Raw events first (M6-M8) [V]

```text
agent event → validation (typed, bounded, no authority fields) → agent_events (Postgres, idempotent, auditable)
            → deterministic candidate → EXISTING governance (duplicate / conflict / supersession / provenance)
            → canonical memory + graph
```

13 event types (+ `memory_proposal`). Every payload model is `extra="forbid"`; text is cleaned (control and
bidirectional-override characters stripped) and bounded; paths must be safe repository-relative paths; the client
cannot supply `user_id`, `project_id`, ownership or database ids; one malformed event rejects the whole request
before anything is written. Timestamps need a timezone and may not be in the future or older than a year.

Governance reused: `ExtractionRunner.apply_structured_candidate` runs grounding, entity-ambiguity flagging,
duplicate/conflict/supersession classification, provenance and the canonical write. It skips only the
memory-worthiness gate (an event is meaningful by construction) and temporal-hint parsing (valid time is the event's
own time). **No LLM is called.** Conflicting proposals are stored as `conflicting`, never overwriting what is on
record. They are visible in the Explorer's conflicting filter but are **not** added to the human review queue (that
queue is bound to extraction jobs) [L].

## 6. Idempotency guarantees [V] (M26)

| Operation | Key | Guarantee |
|---|---|---|
| Project registration | `(owner, identity_key)` unique | One project; concurrent calls resolve through the constraint |
| Agent event | `(session, event_key)`; key = client `event_id` or a content hash | Recorded once; same key + different content → 409 |
| Memory proposal | content hash (no timestamp) | A retried proposal is the same event |
| Event → memory | `uuid5(space, event_id)` + row lock + existence check | Same event → same memory identity; replay, concurrent projection and crash-after-write all converge on one memory |
| Git commit | `(space, sha)` unique; same sha + different content → 409 | Recorded once |
| Commit → memories | `uuid5(space, commit, part)` | Same commit → same memory ids |
| Structure | `uuid5(space, path, structure_hash, part)` | Unchanged file untouched; changed file supersedes the old structure; an older snapshot never overwrites a newer one |

`MemoryService.create()` did **not** safely support a caller-supplied id (its `id_fn` also generates entity and
source ids; reusing it as a constant collided on the first entity — found by a test). It now takes an optional
`memory_id` (default behaviour unchanged); `Memory.id` stays globally unique, so a duplicate id is refused
atomically, never merged or duplicated [V: 6 tests].

## 7. Git ingestion (M4, M5) [V]

`scripts/git_collect.py` (stdlib only) runs **read-only** `git` with fixed argument lists (no shell) and POSTs bounded
JSON. It sends: commit hash, parents, author name, a SHA-256 of the author email (never the address), dates,
subject/body (capped), changed paths with status and add/delete counts, branch membership. It never sends diffs,
file contents, absolute paths or credentials embedded in a remote URL. **The server never reads the filesystem.**

Structure: directories and files from `git ls-tree` at a commit (the committed tree, not the working copy);
Python classes / functions / methods and imports via stdlib `ast`; other languages file-level only. Excludes
(`node_modules`, binaries, lock files, …) are configurable. Graph shape: one memory per commit (+ parts of 20
files), one per file structure, one per directory; structural memories are **not embedded** (keyword and graph
retrieval only). Commit messages are embedded (one vector per commit).

## 8. Provenance (M24) [V]

Every projected fact cites `git_commit` (location `commit` or `file:<path>`) or `agent_event`. The provenance
resolver returns the commit (hash, subject, author, file change) or the event (type, session, times, text);
`resolved:false` when the original is gone. Agent claims are never labelled as Git evidence.

## 9. Failure handling (M27) [V]

Recording (Postgres) and projection (Neo4j) are separate transactions. Neo4j down → the event is accepted and shown
`pending`; it is projected later (inline retry, the sweeper's `project_events` step, or
`POST /projects/{id}/events/project`), in session order, exactly once. Postgres down → nothing is written.
A projection that keeps failing becomes `failed` after `PROJECT_PROJECTION_MAX_ATTEMPTS`. This is **eventual
consistency**: a pending event is not yet searchable but is durable and auditable. Tested: Neo4j down, Postgres
down, malformed/duplicate/oversized events, unknown/ended sessions, expired/revoked tokens, partial Git batches,
lost status update (crash window), concurrent duplicate projection, interrupted (never-ended) sessions.

## 10. Limitations [L]

- Structure parsing is Python-only; other languages are file-level. No call graph (`CALLS`) is produced.
- Deleted files are recorded as commit changes but a snapshot does not retract their old structure memory.
- Reverting a file to a *previous* exact structure after a different snapshot does not re-activate the old memory.
- Ingestion is one Neo4j transaction per memory (see the report's throughput numbers).
- The user's pre-existing local `eval-agent-*` Neo4j nodes were left untouched.
