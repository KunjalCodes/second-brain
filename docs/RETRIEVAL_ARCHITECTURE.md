# Retrieval architecture (Phase 17, M6)

Labels: **[V]** verified, **[I]** inspected in code, **[L]** limitation.

## Paths

| Path | Endpoint / tool | Channels | Temporal | Notes |
|---|---|---|---|---|
| Keyword memory search | `GET /memories/search` | PostgreSQL/Neo4j substring + filters | status/type/time filters | Memories page; exact substring, paginated |
| Hybrid search | `POST /search` | vector (pgvector) + graph + keyword, merged and re-ranked | `current` / `historical` / `as_of` / unspecified | Search page and the agent's `search_memory`; up to 50 results |
| Graph retrieval | `GET /graph/*`, `/entities/*`, `POST /graph/context` | traversal, paths, neighbourhood, context | time lens | always bounded (node/edge caps, hop limits) |
| Agent retrieval | `/conversations/{id}/agent-turn` | the hybrid + graph tools | per tool | model chooses tools; results are untrusted data |
| Project retrieval | `/projects/{id}/memory/search`, `/entity`, `/graph`, `/history`, `/files/history`, … ; MCP tools | SQL over events/commits + project graph; deterministic ranking | `as_of` where meaningful | **no LLM, no embeddings**: structural memories skip embeddings |
| Resume | `GET /projects/{id}/resume`, `get_resume_context` | explicit evidence only | latest session | relevance-gated; `task` is a bias, not a filter |

## Hybrid pipeline [I]

1. Resolve the temporal scope (CURRENT / HISTORICAL / AS_OF / UNSPECIFIED). `valid_from` is
   inclusive, `valid_until` exclusive (half-open).
2. Run the channels independently; each returns scored candidates for **this user only**.
3. Merge by memory id, normalise per-channel scores, add a bounded graph/entity bonus.
4. Re-rank deterministically; label each result with `retrieved_via` and a score breakdown.
5. Return the bounded list. Superseded/historical memories are excluded from CURRENT and
   labelled when the scope asks for them.

## Why project spaces use different retrieval

Project facts are structural (file, commit, decision, error, fix). Exact identifiers matter more
than semantic similarity, and the facts have deterministic ids, so project retrieval uses
indexed SQL and bounded graph reads. Consequences, all documented limitations:

* **[L]** The hybrid *graph channel* is largely inert inside project spaces (few
  free-text entities to anchor on).
* **[L]** Project PostgreSQL scans have no trigram/GIN indexes; fine at measured sizes, to be
  revisited with a measured large project.
* **[L]** There is no cross-space search: personal memory and each project are separate spaces
  by design.
* **[L]** Ranking and term-matching logic exists in three implementations (hybrid retrieval,
  graph intelligence, project query). Behaviour is tested per implementation; they are not
  unified.

## Safety properties [V]

* Every channel filters by `user_id`; project spaces are users, so a project cannot see
  another project or the owner's personal memory (tests: project isolation, auth matrix).
* Retrieved text is returned inside an envelope with a `notice` stating it is data, never
  instructions; the agent's system prompt keeps the same boundary.
* Results are bounded (limits, hop caps, node caps); there is no unbounded graph read.
