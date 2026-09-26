# Phase 10 — Memory Explorer UI & Productization

**Status:** PLAN, awaiting approval. Nothing here is implemented.

**Baseline:** commit `873a409` (Phase 9 complete). Backend: 1258 tests
passing. Frontend: no code.

**Constraints:**
- zero cost;
- no Docker;
- no paid analytics or telemetry;
- backend semantics stay server-side;
- no new backend behaviour unless explicitly approved (section 27).

---

## 1. Executive Summary

The backend can already answer every question the product exists for:

- **what** the AI remembers — memories, entities, relationships;
- **why** — provenance and edge evidence;
- **when** it was true — validity windows, `as_of`, timelines;
- **what replaced it** — supersession, merges, duplicates;
- **what conflicts** with it — `conflicts_with`, the review queue;
- **how** it was retrieved — hybrid search explanations.

None of this is visible to a person yet. Phase 10 builds the **Memory
Explorer**: a Next.js web app that makes the memory architecture legible and
inspectable, **not** a generic dashboard or chat clone.

**Recommendation in one paragraph.**
- **Framework:** a Next.js (App Router, TypeScript, Tailwind) app served on
  the same origin as the API through a Next.js rewrite proxy. This is the
  Phase 9 topology (ADR 0022), and the session stays an HttpOnly cookie.
- **Data fetching:** client-side, via a thin typed client generated from the
  backend's OpenAPI schema, with TanStack Query for caching, pagination and
  mutations.
- **Graph:** Cytoscape.js, chosen for real network-graph needs: layouts,
  parallel edges, hundreds of elements, data-driven styling.
- **Build order:**
  1. a topology spike and auth;
  2. the Memory Explorer and memory detail;
  3. provenance;
  4. entities and temporal views;
  5. the graph explorer;
  6. hybrid search;
  7. documents;
  8. governance/control;
  9. then, optionally, "Ask my memory" and a free-tier demo deployment.
- **Rendering rule:** all memory and document text is rendered as inert
  text.

**Honest limits up front:**
- A **free-tier demo** is possible (Vercel Hobby + Render Free + Neon Free +
  AuraDB Free), but with cold starts, ephemeral disk, auto-pause/deletion
  policies and non-commercial terms. A free, production-grade deployment is
  not realistic (section 22).
- Four small **backend gaps** limit parts of the UX. Each needs an explicit
  decision (section 27).

---

## 2. Current Frontend State

Audited on 2026-09-26.

| Item | Finding |
|---|---|
| `frontend/` | Only `README.md` and `.env.example`. **No `package.json`, no source, no build config.** Nothing to reuse or migrate. |
| `frontend/README.md` | Updated in Phase 9. It requires a Next.js app reading `NEXT_PUBLIC_API_URL` in one module (`lib/api.ts`), session-cookie auth with `credentials: "include"`, no tokens in JS, and a same-origin Next.js proxy. Deployment target is Vercel with `frontend/` as the project root. |
| `frontend/.env.example` | `NEXT_PUBLIC_API_URL=http://localhost:8000` (labelled "Phase 3", stale). The production example points at a separate `*.up.railway.app` host, a cross-site topology that ADR 0022 rejects. The Phase 10 proxy design makes the browser-visible API URL the frontend's own origin (section 15). |
| Root `package.json` | None. |
| CI | `.github/workflows/ci.yml` (backend unit, ruff, audits) and `integration.yml` (manual/weekly). There is no frontend job yet. |
| Backend CORS | Explicit origins only (`FRONTEND_URL`, `CORS_ORIGINS`). An optional single Vercel project's previews. `allow_credentials=True`. Exposed headers: `X-Request-ID`, `Retry-After`. |
| Backend auth | Session cookie `sb_session` (HttpOnly, SameSite=Lax, Secure outside dev). Bearer for scripts. Origin check on cookie-authenticated unsafe methods. Registration off by default in production. |

**Conclusion:** Phase 10 starts from an empty frontend.

---

## 3. Backend Capabilities Available

Every UI capability below maps to an **existing** endpoint. Gaps are listed
separately; the UI must not fake them.

| Capability | Endpoint(s) | Shape notes relevant to UI |
|---|---|---|
| Session bootstrap / login / logout | `GET /auth/me`, `POST /auth/login`, `/auth/logout`, `/auth/logout-all`, `/auth/password`, `/auth/register` | `me` → `{user, session:{expires_at, idle_expires_at}, auth_mode}`. Register returns an unknown-route 404 when disabled. |
| Browse / filter memories | `GET /memories/search?q&status&memory_type&entity&as_of&include_excluded&limit&offset` | `{items: Memory[], total, limit, offset}`. `q` is a case-insensitive **substring**. Hides `invalid`/`archived`/`merged` by default. Ordered newest first. |
| Memory detail | `GET /memories/{id}` | `MemoryDetail`: memory, entities, relationships, sources, `supersedes`, `superseded_by`, `conflicts_with`, `duplicate_of`, `duplicates` |
| Provenance | `GET /memories/{id}/provenance` | `sources[]`: `source` (type, reference, location) + resolved `message` \| `document` + `document_chunk` (≤4000 chars), each nullable when unresolved |
| Control a memory | `PATCH /memories/{id}`, `POST /memories/{id}/invalidate` | Server-validated status transitions (409 on illegal). Invalidation is non-destructive. |
| Hybrid search | `POST /search {query, limit≤200}` | `items[]: {memory: MemoryDetail, explanation:{retrieved_via[], vector_similarity, graph_relevance, keyword_match, temporal_fit, confidence, final_score, temporal_state}}`. **No pagination, no filters.** |
| Entities | `GET /entities/search?q&include_merged`, `/entities/by-name`, `/entities/{id}`, `/{id}/relationships?direction&as_of`, `/{id}/memories`, `/{id}/history?as_of`, `/{id}/graph` | `/{id}` silently resolves merged ids (the UI can compare ids). `history` returns `current[]` / `historical[]` relationships at an instant, matching PRODUCT.md §8. |
| Graph slices | `GET /graph/overview`, `/graph/entities/{id}/neighborhood`, `/graph/edges/evidence`, `/graph/memories/{id}/timeline` | `GraphSlice`: `budget`, `truncated_nodes/edges/memories`, `resolved_from`, `temporal_scope`, `as_of`, nodes/edges with a server-computed `temporal_state`. Evidence: `assertions[]` with an explicit `evidence_status`. |
| Conversations | `POST/GET /conversations`, `GET /conversations/{id}`, `POST/GET /conversations/{id}/messages` | Posting a message queues extraction in the background. |
| Extraction status | `GET /extraction/jobs`, `/extraction/jobs/{id}` | Per-job status for conversation- or document-derived extraction |
| Agent | `POST /conversations/{id}/agent-turn {message}` | `{message, cited_memory_ids (server-validated), tool_call_count, termination_reason, confidence: high\|medium\|low\|not_found}` |
| Documents | `POST /documents` (multipart), `GET /documents`, `/documents/{id}`, `/documents/{id}/chunks` | Formats: **text/plain, text/markdown, text-layer PDF** only. ≤20 MB, ≤200 pages, ≤500 chunks. Status: `uploaded → validating → parsing → chunking → chunked → extracting → complete \| partially_complete \| failed`, plus `error_code` / `error_detail`. Chunks are returned **all at once** (no pagination). |
| Review / governance | `GET /review/memories`, `/review/memories/{id}`, `/review/entities`, `/review/entities/{id}`, `/review/history`; `POST …/confirm-duplicate`, `confirm-distinct`, `resolve-conflict {supersede\|keep_both\|reject, winner}`, `approve-merge {canonical}`, `reject-merge`, `reverse-merge` | Pending candidates only, paginated (limit ≤100, total). Every action takes `if_version` (409 on stale). An audit log is available. |
| Correlation | every response | The `X-Request-ID` header is exposed to the browser. |

**Backend gaps that affect the UI.** None is fabricated around; each needs
a decision (section 27).

| # | Gap | UI consequence if not addressed |
|---|---|---|
| G1 | No way to list the memories **derived from a document** or a conversation. Provenance runs memory → source only. | The document page shows chunks and extraction status, but not "memories learned from this document". |
| G2 | Authenticated responses carry no `Cache-Control: no-store`. | The client sets `cache: "no-store"` on every fetch; the browser/proxy cache stays a residual risk. |
| G3 | No endpoint exposes whether registration is enabled. | The UI shows "Create account" only behind a build flag, and handles the 404 gracefully. |
| G4 | Behind a Vercel proxy, client IPs arrive from Vercel's non-fixed egress addresses, so `TRUSTED_PROXY_IPS` can't be set reliably. | Per-IP login limits degrade to per-egress-IP. The per-email limit still works. A shared-secret proxy header would be a backend change. |

---

## 4. Product Model

These terms are used **consistently** in the UI and mirror backend
semantics. Nothing is called a generic "fact".

| Concept | UI term | Meaning shown to the user | Never conflated with |
|---|---|---|---|
| Memory | **Memory** | One thing the AI stored, in its own words, with a type, status, validity and confidence | Entity; Source |
| Memory type | Semantic / Episodic / Procedural / Working | "What is known" / "what happened" / "how something is done" / "temporary task context" | Status |
| Entity | **Entity** | A person, project, technology, … that memories are about. It has a page. | Memory |
| Relationship | **Relationship** (`Drawing Extractor —uses→ PaddleOCR`) | A typed link between two entities, **asserted by one or more memories** | Memory (a relationship is evidence-backed, not self-standing) |
| Source | **Source** | Where a memory came from: a message, a document chunk, a URL, … | The memory derived from it |
| Provenance | **"Why the AI believes this"** | The chain memory → source(s) → the actual message / document text | Confidence |
| Conversation | **Conversation** | A chat thread whose messages may produce memories | Memory |
| Document | **Document** | An uploaded file, split into **chunks** that may produce memories | Source (a source *points to* a chunk) |
| Valid time | **"True from … until …"** | When the fact held **in the world** (`valid_from` / `valid_until`) | Recorded time |
| Transaction time | **"Recorded …" / "Last changed …"** | When the AI **learned** or last updated it (`created_at` / `updated_at`) | Valid time |
| Current state | **Current** | True now (the window contains now, and the status is active or disputed) | Historical |
| Historical state | **Historical** | Was true; the window closed | Deleted (nothing is deleted) |
| Status: `superseded` | **Superseded** — "replaced by →" | A newer memory replaced it; it is kept | Invalid |
| Status: `invalid` | **Retracted** (backend: *invalid*) | Judged never true; kept, hidden by default | Superseded |
| Status: `conflicting` | **Disputed** | Believed, but contradicted by another memory; unresolved | Superseded |
| Status: `merged` | **Duplicate (merged)** — "see canonical →" | A confirmed duplicate of another memory | Deleted |
| Status: `archived` | **Archived** | Filed away, excluded from retrieval | Retracted |
| Entity merge | **"Merged into →"** | This entity was consolidated into another; its id still resolves | Deletion |
| Confidence | **Extraction confidence 0.92** | The model's own score at extraction time: metadata, **not** a probability of truth | Truth; relevance |
| Relevance | **Relevance (relative)** | A search-ranking score, only comparable within one result list | Confidence |
| Answer confidence | **High / Medium / Low / No memory found** | The agent's self-reported confidence in an answer | Memory confidence |

**`status` vs `temporal_state`.** The status is a **lifecycle** label (what
happened to the memory). The temporal state is **window arithmetic**
(current / historical / not yet valid at the chosen instant). Both are
server-computed and are shown side by side, never merged into one badge.
For example, *Superseded · True Jan–May* is shown as *Historical* now and as
*Current* "as of March".

---

## 5. Information Architecture

| Area | User goal | Backend dependency | Primary interaction | Phase 10? |
|---|---|---|---|---|
| **Sign in / Account** | Get in; manage sessions/password | `/auth/*` | Login form; settings page | ✅ M1 |
| **Overview** (home) | "What does the AI know about me, at a glance?" | `/memories/search` (total + newest), `/review/memories` (total), `/documents` (processing) | Recent memories, counts, "needs review", documents in progress | ✅ M2 (thin) |
| **Memories** (Memory Explorer) | Find and inspect memories | `/memories/search`, `/memories/{id}`, `/provenance` | Filterable, paginated list → detail panel | ✅ M2–M3 (core) |
| **Entities** | See everything about one project/person/tool | `/entities/*` | Entity search → entity page (current vs historical relationships, memories, sources) | ✅ M4 |
| **Graph** | Explore how things connect, incrementally | `/graph/*` | Canvas + detail panel + evidence drawer + time control | ✅ M5 |
| **Search** | "Find what's relevant to X", ranked | `POST /search`, `/entities/search` | Ranked results with "why this result" | ✅ M6 |
| **Documents** | Teach the AI from files; check what happened | `/documents/*`, `/extraction/jobs` | Upload, status, chunks | ✅ M7 |
| **Review** | Resolve duplicates, conflicts, entity merges | `/review/*` | Queue → side-by-side comparison → decision | ✅ M8 (decision Q2) |
| **Ask** (agent) | Ask questions answered from memory, with citations | `/conversations/*`, `/agent-turn` | Chat with verified citations | ⚠️ M9, optional (Q1) |
| **Conversations** (raw) | Read past threads | `/conversations/*` | Thread view, reached from provenance links and Ask | ✅ as a provenance target (M3); full list with M9 |
| **Sources** (standalone) | Browse all sources | — (no list endpoint) | — | ❌ No endpoint. Sources are reached **through** memories and evidence, which matches the data model. |

**Navigation:** a left rail with Overview, Memories, Entities, Graph,
Search, Documents, Review (with a pending-count badge) and Ask (if
enabled), plus the account menu at the bottom. A persistent **global time
control** in the header sets the temporal lens (section 8). Pages that
support `as_of` honour it and say so; pages that don't are labelled "not
time-filtered".

**Routes:**
- `/login`
- `/` (overview)
- `/memories`, `/memories/[id]`
- `/entities`, `/entities/[id]`
- `/graph`
- `/search`
- `/documents`, `/documents/[id]`
- `/review`, `/review/memories/[id]`, `/review/entities/[id]`
- `/ask`, `/conversations/[id]`
- `/settings`

---

## 6. Memory Explorer

**List** (`/memories`, backed by `GET /memories/search`):

- **Search box:** a substring match on memory text, debounced 300 ms. The
  label says "Contains…", **not** "semantic search"; the ranked hybrid
  search lives on `/search`.
- **Filters** (all server-side parameters):
  - Status: multi-select. The default shows the backend default set
    (current, disputed, historical, superseded) and states "Retracted and
    archived hidden". The "Include retracted/archived" toggle maps to
    `include_excluded`.
  - Type: multi-select.
  - Entity: an exact name; the entity page links here pre-filtered.
  - As of: from the global time control, sent as a timezone-aware ISO
    instant.
- **Sorting:** the backend orders newest first (`created_at DESC`). The UI
  labels this "Newest recorded first" and offers **no** other sort. It
  won't sort client-side a page it didn't fully fetch.
- **Pagination:** numbered pages from `total` / `limit` / `offset`, 25 per
  page (the server default; 50/100 selectable, within `SEARCH_MAX_LIMIT`).
  Filters and page are kept in the URL (`?status=…&page=2`). Free text is
  **not** put in the URL (section 20).
- **Row:** the text (two-line clamp), type, **status chip**, validity range,
  "recorded" relative date, extraction confidence, and an entity chips
  preview.

**Detail** (`/memories/[id]`; a side panel on desktop, a full page on
narrow screens):

- **Header:**
  - full text, rendered inertly;
  - type and status chip;
  - temporal state *now* (and *as of X* if a lens is active);
  - "True from Jan 15 2026 until May 1 2026" / "True since Jan 15 2026 (no
    known end)";
  - "Recorded Jan 15 2026 · Last changed May 1 2026";
  - extraction confidence, with an explainer tooltip.
- **Lifecycle strip:** from the link fields:
  - "Supersedes →" / "Superseded by →";
  - "Disputed with →";
  - "Duplicate of →" / "Duplicates of this →".

  Each is a link. "Open timeline" goes to the §8 view.
- **Entities mentioned:** chips linking to entity pages.
- **Relationships asserted by this memory:** `A —predicate→ B`, each with
  its own window and status (edges can differ from the memory, e.g.
  superseded edges). "Show in graph" links to `/graph` centred on A.
- **Why the AI believes this:** provenance (section 9).
- **Actions** (M8): "Retract this memory…" (invalidate, with a reason).
  A confirm dialog states *"Retracted memories are kept but excluded from
  answers."*
- **Debug disclosure:** a collapsed "Technical details" section with the
  memory id and relationship ids, which are copyable. Raw ids never appear
  elsewhere.

**States:**

| State | Shown as |
|---|---|
| Loading | Skeleton rows; never an unbounded spinner |
| Empty (no memories at all) | "The AI hasn't stored any memories for you yet" + links to Ask / Documents |
| Empty (filters) | "No memories match these filters" + "Clear filters" |
| 404 | "This memory doesn't exist, or it isn't yours" (the backend's deliberate ambiguity is preserved) |
| Error | A message + request id + retry |

---

## 7. Graph Explorer

Built **only** on the four Phase 8 endpoints; no new graph queries.

**Layout:** a canvas in the centre, a detail panel on the right, and a
toolbar with the scope and time control, search-to-focus, re-root and a
"list view" toggle.

1. **Entry.** `/graph` with no focus calls `GET /graph/overview`: the most
   recently updated entities plus the in-scope edges among them. The label
   is honest: *"Recently updated entities (up to 150)"*. It must **not** say
   "most important" or "your whole graph"; the ranking is recency (Phase 8
   §25.3).
2. **Focus / expand.** Clicking a node calls
   `GET /graph/entities/{id}/neighborhood?hops=1&exclude_ids=…`.
   - The result is merged into the canvas by id.
   - Expansion is **progressive**: one hop per click. 2-hop is an explicit
     choice; `hops>2` is never requested (the server cap is 2).
   - The client never sends `max_nodes` / `max_edges` / `hops` above the
     `budget` the server reported; normally it omits them and lets the
     server default apply.
3. **Excluding loaded nodes.** `exclude_ids` is capped server-side at the
   node budget (150). Once more than 150 nodes are on the canvas:
   - the client sends the 150 nodes nearest the expanded node, and dedupes
     the response locally by id;
   - past a client canvas cap (proposed 600 elements), the UI asks the user
     to **re-root** rather than growing without bound.
4. **Edge selection.** Clicking an edge opens the evidence drawer via
   `GET /graph/edges/evidence?source_entity_id&target_entity_id&predicate&temporal_scope`.
   - It lists each assertion: the relationship window and status, the
     asserting memory (linked), and its sources.
   - Every assertion shows its **`evidence_status`**, verbatim in meaning:
     *Evidence available* / *No asserting memory* / *Memory not found* /
     *Memory has no source* / *Source recorded but unavailable*.
   - It includes an "Include history" toggle (`temporal_scope=all`).
5. **Memories on the canvas.** A "Show memories" toggle sends
   `include=memories`.
   - Memory nodes are visually distinct from entity nodes.
   - Mentions edges are thin.
   - `supersedes` / `conflicts_with` / `duplicate_of` edges are drawn
     between memory nodes.
6. **Merges.** If a response has `resolved_from`, the UI shows *"‘X’ was
   merged into ‘Y’. Showing Y."* and replaces the URL focus id. "Show merge
   counts" sends `include=merges` and badges nodes with `merged_from_count`.
7. **Truncation (non-negotiable).** When any `truncated_*` flag is true, a
   persistent, non-dismissable banner on the canvas says:

   > *"Partial view — the server stopped at its budget of {budget.max_nodes}
   > nodes / {budget.max_edges} edges. More connected entities exist. Expand
   > a node or narrow the scope to see more."*

   - The UI never shows a total, never says "all", and never auto-requests
     a larger budget.
   - `truncated_memories` gets its own note ("some memories per entity not
     shown").
8. **Time.** The scope control is `Current` (default), `As of [date]` or
   `All time (labelled)`.
   - It maps 1:1 to `temporal_scope` and `as_of`.
   - `as_of` is never sent with `current` (the backend returns 422).
   - Styling comes from the server's `temporal_state` per edge and node:
     - current = solid;
     - historical = dashed and muted;
     - not yet valid = dotted.
   - Disputed status is marked with an icon, not colour alone.
9. **Accessible alternative.** A "List view" renders the same `GraphSlice`
   as tables (entities with hop distance; relationships with state; the
   truncation banner). It serves keyboard and screen-reader users, and is
   the mobile fallback.

**Graph data rules:**
- The client computes **no** temporal logic, identity resolution or
  budgets.
- It maps `GraphSlice` to visual elements and back, and nothing more.
- Edge ids from the server (stable, aggregate-level) are the element ids.

---

## 8. Temporal Memory

**One global lens:** Now (default), or As of *date/time*, chosen in the
header.

| Where the lens applies | Mechanism |
|---|---|
| Memories list | `as_of` parameter |
| Entity page | `/entities/{id}/history?as_of`, `/relationships?as_of` |
| Graph | `temporal_scope=as_of&as_of` |
| Evidence | `temporal_scope=as_of&as_of` |

Pages where the backend has no instant parameter show a "not time-filtered"
chip rather than pretending to be filtered. That covers hybrid search,
documents and conversations.

**Two clocks, never mixed:**
- **Valid time** is "True from … until …". It is drawn as a horizontal bar
  on timelines.
- **Recorded time** is "Recorded …" / "Last changed …", shown as secondary
  metadata.

Tooltips explain the difference once, plainly: *"When it was true" vs "when
the AI learned it"*. A memory recorded in June about something true since
January shows exactly that.

**Memory timeline** (`GET /graph/memories/{id}/timeline`, ≤20 memories):

- a vertical, oldest-first list; the server orders it by valid_from, then
  created_at, then id;
- a valid-time bar per memory on a shared axis;
- connectors for *supersedes* (newer → older), *duplicate of* and *disputes*;
- each node's **stored status** and **temporal state**;
- a truncation note when `truncated_nodes` ("chain continues beyond 20 —
  open an older memory to continue").

It is custom SVG/HTML. At most 20 items, so no library is needed.

**Entity history** (PRODUCT.md §8): *Current* and *Historical* relationship
groups from `/entities/{id}/history`. The date picker re-queries with
`as_of`.

**Explaining "not deleted":** every superseded, retracted, merged or
historical memory is still viewable, with an explicit line:

| Status | Line shown |
|---|---|
| Superseded | *"This memory was superseded on {date} by → … It is kept for history and is used to answer questions about the past."* |
| Retracted | *"… judged not true. Kept for the record; not used in answers."* |

---

## 9. Provenance & Sources

**"Why the AI believes this"** panel (from `GET /memories/{id}/provenance`):

| Source type | Rendering |
|---|---|
| message | The quoted message text (inert), role, time, and "Open conversation →" (`/conversations/{conversation_id}#message-{id}`) |
| document (+ chunk) | Filename, page(s), section heading, the chunk text excerpt, and "Open document →" (`/documents/{id}#chunk-{ordinal}`) |
| url | The URL as **text**. An explicit "Open link" button is shown only for `http(s)` schemes, opening a new tab with `rel="noopener noreferrer"`. Never auto-linked. |
| user_statement / tool_result / system_inference / conversation | Type label + reference + location as recorded. The UI claims **no** more than the backend resolved. |

**Distinguishing the source from the memory derived from it.** Two visual
tiers:
- "The AI stored: *{memory}*" on top;
- "Derived from: *{source}*" beneath.

The source text is never styled as if it were the memory.

**Honesty states:**

| Backend condition | UI |
|---|---|
| `sources: []` | "No source recorded for this memory." It explains that it may have been created directly (e.g. via the API) rather than from a conversation or document. |
| Source present, `message` / `document` null | "A source was recorded, but its content is no longer available." The recorded metadata is shown. |
| Multiple sources | All are listed: "Corroborated by N sources". |
| Conflicting information | Sources do not conflict in the model; **memories** do. The panel links to "Disputed with →" memories and their own provenance, so both sides are inspectable. |
| Edge evidence (graph) | Uses `evidence_status` (section 7), never inferred. |

---

## 10. Memory Review / Governance

The backend supports this fully (Phase 4).

| Queue | Endpoint | Decision UI | Backend action |
|---|---|---|---|
| Likely duplicate / flagged memories | `/review/memories` (kind) → `/review/memories/{id}` | Side-by-side memories A and B (with provenance) + evidence summary (score as "similarity", relative) | **Same thing** → `confirm-duplicate`; **Different** → `confirm-distinct` |
| Conflicts | same | Side by side | **A replaces B** (`supersede` + winner) / **Both true** (`keep_both`) / **Reject** (`reject`) |
| Entity merge candidates | `/review/entities` → `/review/entities/{id}` | Two entity summaries (counts, aliases) | **Merge into A / into B** (`approve-merge`, canonical) / **Keep separate** (`reject-merge`) |
| Past decisions | `/review/history` | An audit log list | **Undo merge** (`reverse-merge`) where the log shows an approved merge |

**Rules:**
- Every action sends `if_version` from the loaded detail. On a 409: *"This
  item changed since you opened it. Reload to see the current state."* The
  UI does not retry silently.
- An optional reason field.
- Consequences are stated before confirming, e.g. *"B will be kept, marked
  as a duplicate of A, and hidden from answers."*
- No flows beyond these endpoints, and no bulk actions.

**Memory control** (outside the queue): "Retract" (invalidate) from memory
detail.

The following stay deferred:
- **Generic PATCH editing** of status, validity or confidence. Legal
  transitions are server rules, and a generic editor would invite
  duplicating them.
- **Manual memory creation** (Q10).

**Recommendation.** Include review in Phase 10 (M8, after the core
explorer), because "control what the AI remembers" is part of the product
question. Candidates only appear when semantic flagging or LLM extraction
runs, so the queue may be empty in a fake-provider demo. It gets a proper
empty state.

---

## 11. Document Experience

Minimal and truthful to Phase 6.

- **Upload:**
  - drag-and-drop or picker;
  - an `accept` list and copy both say **"Plain text, Markdown, or PDF with
    selectable text. Scanned/image-only PDFs aren't supported."**;
  - a client-side pre-check of ≤20 MB (UX only; the server validates);
  - server errors are shown verbatim from `error_code`/`error_detail`
    mapping.

  The Vercel proxy's request-body limit must be **verified in M0/M10**. If
  it is below 20 MB, the UI caps at the verified limit and says so.
- **List:** filename, type, size, pages, uploaded time, and a **status
  pipeline** (uploaded → validating → parsing → chunking → extracting →
  complete).
  - While non-terminal, it polls `GET /documents/{id}` every 3 s, backing
    off to 15 s, and stops at a terminal state or after 10 minutes with
    "still processing — check back later".
  - `partially_complete` explains that some chunks could not be extracted.
  - `failed` shows the error and states the document was **not** learned
    from.
- **Detail:** metadata + chunks (from `/chunks`, all returned at once, up
  to 500):
  - collapsed previews with page and heading;
  - a per-chunk extraction status;
  - expand to read (inert text);
  - long lists use CSS `content-visibility` rather than a virtualization
    dependency.
- **"Memories learned from this document":** **not available** (gap G1).
  The UI says so plainly rather than showing an empty list. The workaround
  is reverse navigation: memory provenance links **to** documents.
- **No** OCR, DOCX, URL ingestion, deletion or re-processing: no such
  backend endpoints.

---

## 12. Search Experience

Separate, clearly labelled modes rather than a fake "universal" search. No
backend endpoint searches documents, conversations or sources.

| Mode | Where | Backend | What it is |
|---|---|---|---|
| **Find memories containing…** | Memories page | `GET /memories/search?q` | Substring filter, paginated, time-lens aware |
| **Search** (ranked, default on `/search`) | `/search` | `POST /search` | Hybrid ranking. Up to 50 results (UI default; the server allows ≤200). No filters, no time lens. |
| **Find an entity** | Entities page; global jump box | `GET /entities/search` | Name/alias substring. Merged-away entities hidden (D2). |

**Result card** (hybrid):
- the memory text, status chip, temporal state, entities and a source
  summary;
- **Relevance**, shown as a relative bar within this result list (not a
  percentage, not comparable across queries);
- extraction confidence.

**"Why this result?"** expander, in plain language mapped from
`explanation`:
- *Matched by: meaning* (vector) · *connected entities* (graph) · *exact
  words* (keyword);
- the per-signal values, with an "advanced" disclosure for the raw numbers;
- *Time fit* explained against `temporal_state`.

A footer note says these are ranking signals, not measures of truth.

---

## 13. Agent / Ask Memory

**Recommendation.** Make this an **optional, last functional milestone
(M9)**, or defer it to Phase 11 (Q1). The Explorer answers the product
question on its own. Agent quality also depends on a real LLM: with
`LLM_PROVIDER=fake` answers are scripted, and the Gemini free tier is
rate-limited.

If included:

- **Thread:** `POST /conversations` creates it; each question is
  `POST /conversations/{id}/agent-turn`.
- **Answer:**
  - the answer text is rendered inertly (**no markdown/HTML rendering in
    Phase 10**, since answers can echo injected content);
  - an answer-confidence badge (High / Medium / Low);
  - `confidence = not_found` shows *"I don't have a memory about that."*
- **Citations:**
  - built **only** from `cited_memory_ids`, which the backend has already
    validated against memories retrieved in this turn;
  - each is fetched with `GET /memories/{id}` and shown as a card with
    status and temporal state, linking to memory detail and provenance;
  - the UI **never** parses ids, links or "sources" out of the answer text,
    and never displays an LLM-authored citation the server didn't return.
- **Degraded turns** (from `termination_reason`):

  | Reason | Message |
  |---|---|
  | `citation_stripped` | *"Some citations couldn't be verified and were removed."* |
  | `budget_exhausted` / `loop_detected` / `turn_timeout` / `malformed_output` | A non-alarming explanation that the answer may be incomplete |

- **Current vs historical:** each citation card shows the memory's temporal
  state. If cited memories disagree (disputed or superseded), the UI marks
  them. The UI never picks a winner; the backend and agent already
  distinguish these.
- **Background learning:** messages sent may produce new memories
  asynchronously. A subtle note links to the extraction status
  (`/extraction/jobs`).

---

## 14. Authentication UX

Respects Phase 9 exactly: cookie only, no tokens in JavaScript.

- **Bootstrap:**
  - the app shell calls `GET /auth/me` once;
  - while pending, a neutral full-shell skeleton renders, with **no**
    protected content;
  - 200 renders the app;
  - 401 redirects to `/login?next=<path>`.
- **Login** (`/login`):
  - email + password → `POST /auth/login` **without** `token_in_body`;
    the browser keeps the cookie;
  - then `me` → redirect to `next`, validated as a same-origin relative
    path starting with `/` and not `//` (open-redirect guard);
  - error mapping:

    | Response | Message |
    |---|---|
    | 401 | "Email or password is incorrect" |
    | 403 `account_disabled` | "This account is disabled" |
    | 429 | "Too many attempts. Try again in {Retry-After}." |
    | 403 `origin_not_allowed` | A deployment-misconfiguration message with the request id |

- **Registration:**
  - The link is shown only when `NEXT_PUBLIC_REGISTRATION_ENABLED=true` at
    build time (G3). That defaults to false, matching production.
  - `POST /auth/register` → on 201, the user is sent to log in
    (registration never logs in).
  - A 404 shows *"Registration is closed. Ask the operator for an
    account."*
- **Session expiry:**
  - no client timer (the idle window slides server-side);
  - **any** 401 from any call clears the query cache and redirects to
    `/login?next=…&reason=expired` ("Your session ended — sign in again").
- **Logout:** `POST /auth/logout` → clear the query cache → `/login`.
  **Logout everywhere:** settings → `POST /auth/logout-all` (confirm) →
  `/login`.
- **Password change:** settings → `POST /auth/password`.

  | Response | Message |
  |---|---|
  | 401 | "Current password is incorrect" |
  | 422 | Policy text: 12–128 characters |
  | 204 | "Other sessions were signed out" |

- **Account page:** email, member since, session expiry times (from `me`),
  logout-all, password change.
- **Route protection:**
  - A client `AuthGate` in the authenticated layout is the real check,
    because `me` is authoritative.
  - Optionally, Next middleware redirects to `/login` when **no** cookie is
    present at all. This is purely a UX optimisation, **not** a security
    boundary: cookie presence ≠ validity.
- **Proxy:** `next.config` `rewrites()` maps `/api/:path*` →
  `${BACKEND_ORIGIN}/:path*`. It is server-side config, not a
  `NEXT_PUBLIC_` variable, so the backend origin is never baked into the
  browser bundle.

  The browser only ever talks to its own origin. `Set-Cookie` from the
  backend lands on the frontend origin (host-only), and the forwarded
  `Origin` must equal the frontend origin, which is listed in the backend's
  `FRONTEND_URL`. All three behaviours are **verified in M0** before
  anything else is built.

---

## 15. Next.js Architecture

- **Framework:** current stable **Next.js App Router + TypeScript**.
- **Rendering:** mostly **client components** for authenticated pages. The
  data is per-user, cookie-authenticated and fetched through the proxy.
  Server-side fetching would need cookie forwarding from the Next server
  (an extra hop, and a second place that handles credentials) for no
  benefit here. Server components are used for the static shell, layout and
  login page.
- **State:**
  - **URL** for navigational state (filters, page, focus id, scope, as_of);
  - **TanStack Query** for server state;
  - React local state for UI.
  - **No** Redux/Zustand.
- **Structure:**

  ```
  frontend/
    app/                        # routes (section 5), error.tsx/loading.tsx per segment
    components/ui/              # shadcn-generated primitives (owned source)
    components/memory|entity|graph|provenance|temporal|documents|review|auth/
    lib/api/                    # generated types, fetch wrapper, error model, query keys
    lib/format/                 # dates (Intl), status/temporal labels — presentation only
    tests/unit, tests/e2e
  ```

- **Configuration:**

  | Variable | Visibility | Purpose |
  |---|---|---|
  | `BACKEND_ORIGIN` | server-only | Used by rewrites |
  | `NEXT_PUBLIC_REGISTRATION_ENABLED` | public | Registration link (default false) |

  `NEXT_PUBLIC_API_URL` from the old README becomes **`/api`** (same
  origin). The README and `.env.example` are updated in M1.

**Dependencies** (each justified; nothing else is added without a stated
reason):

| Dependency | Why needed | Existing alternative? | Runtime / bundle | Maintenance | Cost |
|---|---|---|---|---|---|
| next, react, react-dom | Framework (per README/ADR 0022) | — | baseline | high adoption | free |
| typescript | Type safety across ~70 endpoints | — | dev only | low | free |
| tailwindcss | Tokens and layout without a CSS framework runtime | Plain CSS modules (viable, slower to build) | build-time | low | free |
| shadcn/ui (copied source) + the Radix primitives it pulls **per component used** + clsx/tailwind-merge/cva + lucide-react | Accessible dialog, popover, tabs, tooltip, select, toast without writing ARIA from scratch | Hand-rolled (a11y risk) | small, per component | owned source; Radix is well maintained | free |
| **@tanstack/react-query** | Caching and dedupe across panels, paginated queries, mutation + invalidation for review/invalidate, per-error retry policy (no retry on 4xx, honour `Retry-After`), cache clear on logout | Hand-written fetch hooks (would re-implement exactly this) or SWR (weaker mutations) | ~13 KB gz | high adoption | free |
| **cytoscape** (graph route only, lazy-loaded) | Section 16 | — | ~110–130 KB gz, **loaded only on /graph** (to be measured) | stable, long-lived | free |
| **openapi-typescript** (dev) | Generates TS types from the backend's `/openapi.json`, so API drift fails `tsc` | Hand-written types (drift risk) | dev only, zero runtime | low | free |
| vitest, @testing-library/react, @testing-library/user-event, jsdom (dev) | Unit and component tests | — | dev only | low | free |
| @playwright/test, @axe-core/playwright (dev) | E2E against a real local backend; a11y smoke | — | dev only | medium | free |

**Explicitly not added:**
- zod: backend responses are trusted and typed;
- react-hook-form: only login, password and upload forms;
- date-fns: `Intl.DateTimeFormat` suffices;
- a markdown renderer: forbidden by the rendering rule;
- D3, MSW, Storybook, state libraries, analytics SDKs.

---

## 16. Visualization Technology

Requirements from sections 7 and 8:
- ≤150 entity nodes plus up to ~600 canvas elements after expansion;
- **parallel edges** (one aggregate edge per status and temporal state
  between the same pair);
- automatic layout;
- data-driven styling by `temporal_state` / `status` / `kind`;
- node and edge selection;
- incremental add (merge by id) without re-laying out everything;
- labels;
- pan and zoom;
- a keyboard-accessible alternative (the list view);
- small maintenance cost.

| | React Flow (@xyflow/react) | **Cytoscape.js** | D3 (force + SVG) | Custom SVG/canvas |
|---|---|---|---|---|
| Model fit | Node-based editors / flowcharts; nodes are React components | **Network graphs**; the model matches `GraphSlice` directly | A toolkit, not a graph component | Anything, at high cost |
| Layout | None built in (needs dagre/elk/d3-force: another dependency) | **Built-in** (cose, concentric, breadthfirst, grid). Incremental layout of new nodes around a fixed selection is supported. | Force simulation included | Must write |
| Parallel edges | Overlap by default; needs custom edge math | **Native** (bezier / haystack) | Must write | Must write |
| Performance at 150–600 elements | DOM/React per node; OK, heavier | **Canvas renderer**; comfortable at thousands | SVG; OK at hundreds, tuning needed | Depends |
| Styling | CSS/React per node (very flexible) | **Declarative stylesheet with data selectors** (`edge[temporal_state="historical"]` → dashed) | Manual | Manual |
| Selection, pan/zoom, events | Good | Good | Manual | Manual |
| Temporal styling | Custom | Selector-driven, trivial | Manual | Manual |
| Extensibility | React-native nodes (rich HTML cards) | Extensions ecosystem (e.g. fcose); compound nodes | Unlimited | Unlimited |
| Complexity for this app | Medium-high (layout + parallel edges) | **Low-medium** (a thin React wrapper we own) | High | Very high |
| Bundle | ~50 KB gz + layout lib | ~110–130 KB gz, lazy on one route | Modular | — |

**Recommendation: Cytoscape.js**, wrapped in a small owned React component,
not a third-party wrapper.
- It matches the network-graph shape, gives parallel edges and layouts
  without extra libraries, and its selector styling maps 1:1 to server
  fields.
- It is lazy-loaded so only `/graph` pays for it.

React Flow would be the right call for editing node cards; that is a
non-goal (read-only). The **timeline** (≤20 items) is custom SVG/HTML, and
no library is justified.

---

## 17. Design System / Responsive UX

**Character:** technical, trustworthy, calm, data-dense.
- A neutral palette with **one** accent.
- Monospace for ids, timestamps and predicates.
- Status chips with **icon + text** (never colour alone):
  - Current ●;
  - Disputed ⚠;
  - Historical ◌;
  - Superseded ↦;
  - Retracted ⊘;
  - Duplicate ⧉;
  - Archived ▣.
- Light and dark themes via Tailwind CSS variables.
- It must not look like a chat product: chat is one secondary page.

**Layout:**

| Width | Arrangement |
|---|---|
| **≥1280 px** | Three panes: nav rail · main (list or canvas) · detail panel (resizable) |
| 768–1279 px | The detail panel becomes a right drawer |
| <768 px | Single column. The detail is its own page. The graph opens in **list view** by default, with a "canvas works best on larger screens" hint; the canvas remains optional. |

**Keyboard:**
- `/` focuses search;
- `g m` / `g e` / `g g` go to Memories / Entities / Graph;
- `Esc` closes panels;
- arrow keys move within lists, `Enter` opens.

Every canvas action has a list-view equivalent.

**Accessibility:**
- target WCAG 2.1 AA;
- focus rings;
- Radix primitives for dialogs and menus;
- an `aria-live` region for async status (document processing, truncation
  notices);
- respects `prefers-reduced-motion` (no animated layouts).

---

## 18. API Client Contract

`lib/api` is the **only** place `fetch` is called.

- **Types** are generated from `/openapi.json` (`openapi-typescript`) into
  `lib/api/schema.d.ts`, committed and regenerated by a script. CI runs
  `tsc` so backend drift fails loudly.
- **Transport:**
  - `fetch("/api" + path, {credentials: "include", cache: "no-store", …})`;
  - JSON bodies; multipart only for upload;
  - an `AbortController` per query (cancelled on navigation);
  - timeouts: 30 s default, 90 s for agent turns (backend turn timeout is
    60 s, plus proxy and cold-start headroom), 120 s for uploads.
- **Error model:** every non-2xx becomes
  `ApiError{status, code, message, details, requestId, retryAfter?}`,
  parsed from the backend envelope `{error:{code,message,details,request_id}}`
  or from FastAPI's 422 shape. It is handled centrally:

| Status | Handling |
|---|---|
| 401 | Global: clear cache → `/login?next&reason=expired`. Never retried. |
| 403 `origin_not_allowed` | "Configuration problem" page with the request id (a deployment bug, not user error) |
| 403 `account_disabled` | Sign out, with a message |
| 404 | Per-screen "doesn't exist or isn't yours" (the backend's deliberate ambiguity is preserved) |
| 409 | Surface the server message. Review/PATCH: "changed since you opened it — reload". Illegal transition: show why. |
| 422 | Field errors mapped to form fields; otherwise a validation message |
| 429 | Show `Retry-After` countdown; queries auto-retry **once** after it; mutations never auto-retry |
| 5xx / 503 | "Service unavailable" + request id + manual retry; queries retry ≤2 with backoff |
| Network failure | Offline banner; retry on reconnect |

- **Domain states that are not errors** are passed through **typed and
  unmodified**:
  - graph `truncated_*` flags and `budget`;
  - `resolved_from`;
  - evidence `evidence_status`;
  - provenance nulls;
  - agent `termination_reason` / `confidence`;
  - document `status` / `error_code`.

  The client never "fixes up" data.
- **No duplicated business logic:**
  - no status-transition tables;
  - no temporal arithmetic (`temporal_state` comes from the server);
  - no identity resolution;
  - no budget math beyond **not exceeding** the server-reported `budget`.

  `lib/format` only maps values to labels and icons.

---

## 19. Error / Loading / Empty States

Every screen implements all states that apply. There are no blank screens
and no unbounded spinners: every loader has a skeleton and every long
operation has a timeout message.

| State | Presentation |
|---|---|
| Loading | Skeletons in the final layout shape. The auth check shows the neutral shell. |
| Empty | Specific copy + next action (e.g. "No memories yet — ask a question or upload a document") |
| Error (5xx/network) | Message + request id + Retry |
| Unauthorized (401) | Redirect to login with the reason |
| Forbidden (403) | Origin / disabled messages (section 18) |
| Not found (404) | "Doesn't exist or isn't yours" + link back |
| Rate limited (429) | Countdown from `Retry-After`; content stays visible |
| Partial / truncated | Persistent banner (graph, timeline, memories-per-entity); never hidden |
| Ingestion pending | Status pipeline + polling note + "you can leave this page" |
| Ingestion failed | Error code/detail in plain language; "not learned from" |
| Partially complete | Which chunks failed; what was still learned |
| Provenance unavailable | "No source recorded" vs "source content unavailable" (section 9) |
| No memory found (search/agent) | "No memories match" / "I don't have a memory about that" |
| Evidence unavailable (graph edge) | `evidence_status` wording (section 7) |
| Registration closed | Operator message |
| Stale review item (409) | Reload prompt |

---

## 20. Security Model

- **XSS: content is data.** Memory text, entity names, chunk text, message
  text, agent answers and URLs are user- or attacker-influenced. Documents
  are untrusted (ADR 0007), and prompt-injection text *will* appear. The
  controls:
  - rendered only as React text nodes (auto-escaped), with `white-space:
    pre-wrap`;
  - **`dangerouslySetInnerHTML` is banned** by lint (`react/no-danger`) and
    a CI grep;
  - no markdown/HTML rendering of any backend content;
  - no auto-linking;
  - URLs are opened only via an explicit button, `http(s)` only,
    `rel="noopener noreferrer"`;
  - Cytoscape labels are canvas text, which never interprets HTML. **No
    HTML-label extensions.**
- **CSP** (via `next.config` headers), measured and tightened in M1:
  - `default-src 'self'`;
  - `connect-src 'self'` (the API is same-origin);
  - `img-src 'self' data:`;
  - `object-src 'none'`;
  - `frame-ancestors 'none'`;
  - `base-uri 'self'`;
  - `form-action 'self'`;
  - scripts `'self'` + Next's required nonce/hashes. No `eval`.

  Also `Referrer-Policy: same-origin` and `X-Content-Type-Options: nosniff`.
- **Cookies:**
  - the session stays **HttpOnly**; JavaScript never reads or stores it;
  - no `localStorage` / `sessionStorage` for credentials or memory content;
  - the query cache is in memory only and cleared on logout and on 401.
- **CSRF:** a same-origin topology + `SameSite=Lax` + the backend Origin
  check (Phase 9). The browser sends `Origin` on POST automatically. No
  CSRF-token framework is needed.
- **CORS:** not needed with the proxy (same origin). The backend's
  explicit-origin CORS remains as a second layer.
- **Proxy:**
  - only `/api/*` is rewritten, and only to `BACKEND_ORIGIN`;
  - the proxy adds no credentials of its own;
  - the backend origin is not exposed in client code.
- **Caching:**
  - every API fetch uses `cache: "no-store"`;
  - authenticated pages are client-rendered (not statically cached);
  - Next is configured so `/api/*` is never cached at the edge;
  - residual risk: the backend doesn't send `Cache-Control: no-store` (gap
    G2).
- **Sensitive data in URLs:**
  - ids in URLs are fine: they aren't secrets, and ownership is enforced
    server-side;
  - **free-text search queries are not put in the browser URL** (history,
    shared links);
  - the backend contract makes `GET /memories/search?q=` a query string,
    which proxy/host request logs may record. Hybrid search uses the `POST`
    body. This is documented as residual risk.
- **Graph data exposure:** slices are per-user and bounded server-side. The
  client renders what it gets and never requests unbounded data.
- **Open redirect:** the `next` parameter is validated (section 14).
- **Dependency hygiene:** a minimal dependency set, a lockfile committed,
  and `npm audit` in CI (free).

---

## 21. Performance

**Budgets** (verified with Lighthouse and `next build` output; numbers are
targets to measure, not claims):

| Area | Budget |
|---|---|
| Initial JS for authenticated shell + memories route | ≤ 180 KB gz, excluding the lazy graph chunk |
| LCP, desktop, production build, local backend | ≤ 2.0 s |
| Graph route extra chunk | Measured (Cytoscape ~110–130 KB gz expected); loaded only on `/graph` |
| First graph paint for ≤150 nodes / ≤400 edges (layout included) | ≤ 500 ms on the dev machine |
| Canvas cap | 600 elements, then prompt to re-root |
| Memory list page | 25 rows default; server-paginated (no infinite scroll: exact navigation with `total`) |
| Hybrid search | 50 results, rendered without virtualization |
| Document chunks | ≤500, collapsed previews + `content-visibility: auto`; no virtualization library |
| Timeline | ≤20 items; no virtualization |
| Query cache | `staleTime` 30 s for lists, 5 min for immutable details (e.g. provenance); garbage-collected on route leave |

**Rules:**
- **No over-fetching to compensate for the backend.** The client never
  requests above server budgets, and never loops requests to assemble a
  "full" graph.
- Progressive expansion is the design.
- Graph virtualization is not needed at these caps.
- Backend latency (Phase 8/9 measured ≤ ~1.3 s p50 worst case for 2-hop at
  10k) is handled with skeletons and cancellation, not prefetch storms.

---

## 22. Deployment Options

Free-tier terms below were checked on 2026-09-26 and **change often**. They
must be re-verified at M10. *No component is "free forever" at production
scale.*

| Tier | Frontend | Backend | PostgreSQL (+pgvector) | Neo4j | Document bytes | LLM |
|---|---|---|---|---|---|---|
| **Development** | `next dev` (localhost:3000, rewrite → :8000) | local uvicorn | local | local Community | local filesystem | fake or Gemini free key |
| **Free-tier demo** | **Vercel Hobby.** Free, **non-commercial only**. External rewrites time out at 120 s. Monthly quotas apply. | **Render Free web service.** 750 instance-h/month, **spins down after 15 min idle, ~1 min cold start**, **ephemeral filesystem**, no persistent disk. | **Neon Free.** 0.5 GB/project, 100 CU-h/month, scales to zero after 5 min, pgvector available. | **AuraDB Free.** 200k nodes / 400k relationships, **paused after 3 days without writes, deleted 30 days after pausing**, 1 instance, no card. | Ephemeral local disk on Render (lost on restart/redeploy). Chunks live in Postgres, so provenance survives; raw bytes don't. | fake (no real learning) or Gemini free tier (rate-limited) |
| **Production-scale** | Vercel Pro or equivalent (commercial use) | A paid always-on instance, or a small VPS | A paid managed Postgres | AuraDB Professional or self-hosted | Object storage (S3/R2-compatible; free allowances usually need a billing profile) | A paid LLM tier |

**Demo-specific consequences** (these must be designed for, not
discovered):

1. **Cold starts.** The first request after 15 minutes of idle takes about
   a minute on Render. The UI shows a *"Waking the server…"* state when
   `/auth/me` is slow, rather than a generic error.
2. **Background work.** Extraction and ingestion run in-process
   (`BackgroundTasks`). A spin-down can interrupt them. Recovery uses the
   existing `POST /internal/extraction/sweep` (and ingestion sweep) with
   `X-Internal-Token`, so a scheduled caller is needed. GitHub Actions cron
   is free for public repositories; limits must be verified.
3. **AuraDB auto-pause** after 3 days without *writes*. A demo needs either
   periodic real use or a scheduled harmless write. Otherwise it pauses,
   and is deleted after 30 days.
4. **Documents on ephemeral disk.** Uploads survive only until the next
   restart. Either disable upload in the demo, or accept that re-processing
   is impossible after a restart.
5. **Per-IP rate limiting behind Vercel** (gap G4). Egress IPs aren't
   fixed, so `TRUSTED_PROXY_IPS` can't be configured reliably.
6. **Compatibility to verify** at M10:
   - the Cypher features used (`COLLECT {}` subqueries, `CALL (…) {}`) on
     AuraDB's version;
   - pgvector version and HNSW support on Neon;
   - the Vercel request-body limit for 20 MB uploads through rewrites.
7. **Terms.** Vercel Hobby is personal/non-commercial. A public portfolio
   demo fits; anything commercial doesn't.

**Recommendation:**
- Phase 10 **ships and verifies locally first**.
- A free-tier demo deployment is the final, optional milestone (Q8), with
  these limits written in the README.
- A production deployment is **out of scope** and is not free.

---

## 23. Milestone Breakdown

Ordered by dependency and product value. The core Explorer comes first; the
graph comes after memory, provenance and entities exist to link to. Each
milestone ends with green tests and a demo of its acceptance criteria.

**M0 — Topology spike (gate, ~1 day)**
- **Objective:** prove the Phase 9 browser model works through a Next
  rewrite before building on it.
- **APIs:** `/auth/login`, `/auth/me`, one POST (e.g.
  `/memories/{id}/invalidate` on a test memory, or logout).
- **Checks:**
  - `Set-Cookie` passes through and lands host-only on `localhost:3000`;
  - the forwarded `Origin` passes the backend CSRF check with
    `FRONTEND_URL=http://localhost:3000`;
  - what `X-Forwarded-For` the backend sees;
  - upload body size through the dev rewrite;
  - error envelope and `X-Request-ID` visibility.
- **Deliverable:** a short findings section appended to this plan. No
  product UI.
- **Risk:** if rewrites don't pass headers as expected, fall back to a
  Route Handler proxy. That is a decision point.

**M1 — Frontend foundation + authentication**
- **Objective:** a runnable app, the auth flows and the shared plumbing.
- **APIs:** `/auth/*`.
- **Components:**
  - the Next app, Tailwind tokens, shadcn primitives;
  - the app shell (nav, header, time-control placeholder);
  - the `lib/api` client (generated types, error model, 401/429 handling);
  - login, settings (me, password, logout-all);
  - `AuthGate`, error/loading boundaries;
  - CSP headers;
  - a frontend CI job (typecheck, lint incl. `no-danger`, unit tests,
    build).
- **Tests:**
  - API client error mapping (unit);
  - login/logout/expiry redirects (component);
  - open-redirect guard;
  - E2E: login → me → logout against a local backend.
- **Acceptance:**
  - an unauthenticated visit redirects to login;
  - login works via cookie only;
  - a 401 mid-session returns to login with a reason;
  - no token is ever readable in JS (asserted in E2E via `document.cookie`);
  - CI is green.
- **Risks:** proxy details (retired by M0).

**M2 — Memory Explorer + memory detail + overview**
- **APIs:** `/memories/search`, `/memories/{id}`, `/review/memories`
  (count only), `/documents` (count/processing).
- **Components:** the memory list (filters, pagination, URL state),
  status/temporal chips, memory detail (lifecycle links, entities,
  relationships), the overview page.
- **Tests:**
  - filter → query-parameter mapping;
  - status-label mapping for all 7 statuses;
  - empty/404/error states;
  - E2E: seeded data (`scripts/seed_demo.py` + a CLI account attached to
    `demo-user`) → find the EasyOCR memory → see it superseded by PaddleOCR.
- **Acceptance:**
  - every section 6 element is visible;
  - retracted/archived are hidden by default with an explicit toggle;
  - valid time vs recorded time are both shown and labelled.

**M3 — Provenance & sources**
- **APIs:** `/memories/{id}/provenance`, `/conversations/{id}`,
  `/documents/{id}`, `/documents/{id}/chunks`.
- **Components:** the provenance panel, conversation thread view (anchor to
  message), document chunk view (anchor to chunk).
- **Tests:** every honesty state (none / unresolved / multiple); URL
  sources render as text with a safe button; injected-HTML fixture renders
  inert.
- **Acceptance:** from any memory, the user reaches the exact message or
  chunk it came from, or sees an explicit reason why not.

**M4 — Entities & temporal**
- **APIs:**
  - `/entities/search`, `/entities/{id}`;
  - `/entities/{id}/history?as_of`, `/entities/{id}/memories`;
  - `/graph/memories/{id}/timeline`;
  - `as_of` on `/memories/search`.
- **Components:** the entity page (current/historical relationships, like
  PRODUCT.md §8), merged-id notice, the global time control, the memory
  timeline (SVG).
- **Tests:**
  - an `as_of` instant is sent timezone-aware, and never with `current`;
  - "not time-filtered" chip on non-supporting pages;
  - timeline ordering from the server is preserved;
  - truncation note;
  - E2E: "What was I using in March?" → EasyOCR current as of March.
- **Acceptance:** the user can answer "what now" and "what in March" from
  the UI alone.

**M5 — Graph Explorer**
- **APIs:** `/graph/overview`, `/graph/entities/{id}/neighborhood`,
  `/graph/edges/evidence`.
- **Components:**
  - the Cytoscape wrapper (lazy);
  - GraphSlice → elements mapper;
  - the scope/time toolbar;
  - the evidence drawer;
  - the truncation banner;
  - merged notice;
  - list view;
  - canvas cap and re-root.
- **Tests:**
  - the mapper (unit): ids, styles, parallel edges, merge by id;
  - the banner appears **iff** a `truncated_*` flag is set;
  - requests never exceed `budget`;
  - `exclude_ids` capped at the budget;
  - E2E: overview → expand → edge → evidence → memory → provenance.
- **Acceptance:** section 7 in full. No truncated slice is presented as
  complete.
- **Risks:** layout quality and performance, measured against section 21.

**M6 — Hybrid search**
- **APIs:** `POST /search`, `/entities/search`.
- **Components:** the search page, result cards, "why this result",
  entity jump box.
- **Tests:** explanation mapping; relevance shown as relative; no-result
  state.
- **Acceptance:** results explain their channels in plain language, with
  the "signals, not truth" note.

**M7 — Documents**
- **APIs:** `/documents` (upload/list/get/chunks), `/extraction/jobs`.
- **Components:** upload with format copy, status pipeline with bounded
  polling, failure/partial states, the chunk viewer.
- **Tests:**
  - client pre-check;
  - polling stops at terminal states and at the time cap;
  - error-code mapping;
  - E2E: upload a Markdown file → complete → provenance from a resulting
    memory links back.
- **Acceptance:** section 11. The G1 limitation is stated, not hidden.

**M8 — Governance & control**
- **APIs:** `/review/*` (all), `POST /memories/{id}/invalidate`.
- **Components:** review queues, side-by-side comparison, decision dialogs
  with consequences, audit history (incl. undo merge), retract action.
- **Tests:**
  - `if_version` sent;
  - 409 reload flow;
  - every decision maps to the right endpoint and body;
  - retract confirm;
  - E2E on a seeded conflict pair.
- **Acceptance:** section 10. No action without a stated consequence.
- **Decision:** Q2.

**M9 — Ask my memory (optional)**
- **APIs:** `/conversations/*`, `/conversations/{id}/agent-turn`,
  `/memories/{id}`, `/extraction/jobs`.
- **Components:** the Ask page, citation cards, degraded-turn notices,
  not-found state.
- **Tests:**
  - citations come only from `cited_memory_ids`;
  - an answer containing a fake memory id/link renders as plain text and
    produces no citation card;
  - all `termination_reason` messages;
  - E2E with the backend's fake LLM.
- **Acceptance:** section 13.
- **Decision:** Q1.

**M10 — Free-tier demo deployment (optional)**
- **Services:** Vercel, Render, Neon, AuraDB (section 22).
- **Work:**
  - deployment docs;
  - env matrix;
  - backend production settings (`APP_ENV=production`, `FRONTEND_URL`,
    Secure cookie, registration off, CLI account);
  - sweep scheduler;
  - an AuraDB keep-alive decision;
  - cold-start UX;
  - verification of the section 22 compatibility items.
- **Acceptance:** a public demo URL, with limits documented in the README.
  No claim of production readiness.
- **Decision:** Q8/Q9.

---

## 24. Testing Strategy

Minimal, free, and targeted at the risks.

| Layer | Tooling | Covers |
|---|---|---|
| Types | `tsc --noEmit` over generated OpenAPI types | Backend contract drift |
| Lint | Next ESLint + `react/no-danger` + a CI grep for `innerHTML` / `dangerouslySetInnerHTML` | XSS rule |
| Unit | Vitest | API client error mapping and retry policy; `next` redirect validation; status/temporal label mapping; GraphSlice→Cytoscape mapper; budget/`exclude_ids` capping; polling state machine |
| Component | Testing Library (jsdom) + a **fake transport** injected into the API client (no MSW) | Loading/empty/error/401/403/404/409/422/429 states per screen; truncation banner; provenance honesty states; inert rendering of an injected-HTML fixture; citation rules |
| E2E | Playwright against the **real local backend** in session mode, seeded via `scripts/seed_demo.py` + `scripts/manage_users.py` (test account attached to `demo-user`) | Login/logout/expiry; explorer → detail → provenance → source; as-of March; graph expand → evidence; document upload → complete; review decision; cross-user (a second account sees 404/empty) |
| Accessibility | `@axe-core/playwright` on every route; a keyboard-only E2E path through list view | WCAG AA regressions |
| Responsive | Playwright at 1440 / 1024 / 390 widths | Layout breakpoints; graph → list fallback on mobile |
| Performance | `next build` size report + Lighthouse (local, free) at M1/M5/M10 | Section 21 budgets |

**CI:**
- **`frontend-ci`** on every push/PR: typecheck, lint, unit/component
  tests, build. No services needed.
- **E2E** runs manually or weekly beside the existing `integration.yml`,
  since it needs Neo4j and Postgres. It never runs a real LLM.

---

## 25. Explicit Non-Goals

Not in Phase 10:

- a mobile native app;
- a browser extension;
- teams, sharing or public links;
- RBAC, an admin console, billing;
- external OAuth / social login;
- real-time collaboration;
- **graph editing of any kind** (the graph API is read-only by design);
- generic memory field editing (PATCH);
- manual memory authoring (unless Q10);
- bulk review actions;
- a standalone sources browser (no endpoint);
- cross-type universal search (no endpoint);
- document deletion or re-processing, OCR, DOCX or URL ingestion;
- cloud object-storage migration;
- server-side rendering of user data;
- offline mode;
- i18n (English only; dates are localised with `Intl`);
- analytics or telemetry SDKs;
- production-scale infrastructure;
- any backend change not explicitly approved in section 27.

---

## 26. Proposed ADRs

These are to be written during implementation, not now.

| ADR | Title | Records |
|---|---|---|
| 0025 | The Memory Explorer is a client-rendered Next.js app behind a same-origin rewrite proxy | App Router, client data fetching, no SSR of user data, TanStack Query, OpenAPI-generated types, `lib/api` as the single fetch point |
| 0026 | The browser auth boundary is the session cookie alone | No tokens in JS or storage; `me` bootstrap; the global 401 policy; middleware is UX-only; the open-redirect guard; M0 findings |
| 0027 | Graph visualization uses Cytoscape.js with an owned wrapper | The section 16 comparison; lazy loading; selector styling from server fields; list-view accessibility; the canvas cap |
| 0028 | Temporal presentation: valid time and recorded time are different clocks; status and temporal state are different axes | Vocabulary (section 4); the global lens; "not time-filtered" labelling; no client temporal computation |
| 0029 | All backend content is rendered as inert text | The ban on HTML/markdown rendering, the lint rule, CSP, URL handling, and the prompt-injection rationale |
| 0030 | Free-tier demo topology and its limits | The section 22 matrix, the cold-start UX, the sweep scheduler, AuraDB keep-alive, the ephemeral-document caveat, non-commercial terms |

---

## 27. Risks & Open Questions

**Risks**

| Risk | Mitigation |
|---|---|
| Rewrite proxy doesn't pass `Set-Cookie`/`Origin` as assumed | M0 gate; Route Handler proxy fallback |
| Graph looks like a hairball or misleads | Progressive expansion; honest labels (recency, truncation); list view; canvas cap |
| Users misread confidence or relevance as truth | Explicit vocabulary and tooltips (section 4); relative relevance display |
| Temporal confusion (valid vs recorded) | Two labelled clocks; one lens; ADR 0028 |
| Prompt-injection text in UI leads to XSS or phishing links | Inert rendering; no auto-links; CSP (section 20) |
| Scope creep (Ask, deployment) delays the core Explorer | Optional milestones M9 and M10 last |
| Empty review queue in fake-LLM demos looks broken | Designed empty states explaining when candidates appear |
| Free-tier demo instability (cold starts, pauses, ephemeral disk) | Section 22 designs; documented limits; demo only |
| Frontend drifts from backend contract | Generated types + `tsc` in CI |

**Open questions requiring approval**

| # | Question | Recommendation |
|---|---|---|
| Q1 | Include **Ask my memory** (agent chat) in Phase 10 as M9, or defer to Phase 11? | Optional M9; defer if time-boxed |
| Q2 | Include **review/governance actions** (M8), or read-only / defer? | Include (it is "control") |
| Q3 | **Backend gap G1** (list memories derived from a document/conversation): allow a small, tested backend addition in Phase 10 (e.g. a `source_document_id` filter), or accept the limitation? | Accept for M7; separate approved backend task if wanted |
| Q4 | **Gaps G2/G3/G4** (`Cache-Control: no-store`; a registration capability endpoint; a trusted-proxy shared-secret header for client IPs behind Vercel): approve small backend tasks, or accept the documented limitations? | G2 is worth doing (tiny, security); G3/G4 accept for now |
| Q5 | Approve **Cytoscape.js**? | Yes (section 16) |
| Q6 | Approve **TanStack Query** and **openapi-typescript**, and the section 15 dependency list overall? | Yes |
| Q7 | Keep free-text search **out of the browser URL**? | Yes (privacy over shareability) |
| Q8 | Is the **free-tier demo deployment** (M10) in Phase 10 scope, or local-only now? | Optional M10 |
| Q9 | **LLM for the demo:** fake (no real learning) or Gemini free tier (rate-limited, needs a key)? | Decide at M10 |
| Q10 | **Manual memory creation** UI (`POST /memories`): defer? | Defer |
| Q11 | **Registration link:** build-time flag (default off, recommended) vs a new capability endpoint (G3)? | Build-time flag |

---

## 28. Acceptance Criteria

Phase 10 (M0–M8; M9/M10 if approved) is complete when all of these hold:

1. **Auth:** login, logout, logout-all, password change and expiry work via
   the HttpOnly cookie through the same-origin proxy. No credential is ever
   readable by JavaScript or stored in web storage (E2E-asserted).
2. **Explorer:** a user can find any memory, and see its status, type,
   validity (valid time) and recorded time, entities, relationships,
   confidence and lifecycle links (supersedes / superseded by / disputed /
   duplicate).
3. **Provenance:** from any memory, the user reaches the exact source
   message or document chunk, or sees an explicit, backend-accurate reason
   why not.
4. **Temporal:** "what is true now" and "what was true on date X" are both
   answerable in the UI. Valid time and recorded time are never conflated.
   `as_of` requests are timezone-aware and never sent with `current`.
5. **Graph:** built only on the four Phase 8 endpoints. Truncation is
   always visibly communicated when flagged. The client never requests
   beyond the server budget. Merged ids show a redirect notice. An
   accessible list view exists.
6. **Search:** hybrid results explain why they appeared, without presenting
   scores as truth. Substring filtering and ranked search are clearly
   distinct.
7. **Documents:** only supported formats are offered. Status, failure and
   partial states are shown. Polling is bounded.
8. **Governance** (if approved): every review decision maps to an existing
   endpoint with `if_version` and a stated consequence. 409s prompt a
   reload.
9. **States:** every screen implements the section 19 states. No blank
   screens or unbounded spinners.
10. **Security:** no `dangerouslySetInnerHTML` or HTML/markdown rendering
    of backend content (lint + test). CSP is enabled. An injected-HTML
    fixture renders inert. The open-redirect guard is tested.
11. **Contract:** types are generated from the backend OpenAPI; `tsc`,
    lint, unit and component tests pass in CI. E2E passes against a local
    backend, including a cross-user check.
12. **Performance:** the section 21 budgets are measured and reported with
    real numbers.
13. **Accessibility:** axe reports no serious or critical violations on any
    route. A keyboard-only path exists for every core task.
14. **Zero-cost:** no paid service, analytics or telemetry. All new
    dependencies are justified in section 15.
15. **Docs:** `frontend/README.md`, `frontend/.env.example`, `docs/API.md`
    (frontend topology notes) and ADRs 0025–0030 are updated. The README
    status table reflects Phase 10.

---

## 29. Implementation Status

Implemented locally on 2026-09-26 against checkpoint `873a409`.
**Not deployed.** Every number below was measured on one machine:

* Apple M1, 8 GB RAM, macOS (arm64);
* Node 26, Chromium headless shell (Playwright 1.63);
* production `next build` + `next start`;
* local uvicorn in session mode, local PostgreSQL test database, local
  Neo4j Community.

These are relative numbers, not capacity claims.

### 29.1 Milestones

| Milestone | Status |
|---|---|
| M0 proxy / cookie / CSRF spike | Done. Findings in 29.2, turned into E2E tests |
| M1 foundation + auth UI | Done: login, logout, `me` bootstrap, protected routes, global 401, expiry notice, safe redirect-after-login, registration flag, settings (password change, sign out everywhere) |
| M2 overview + Memory Explorer + detail | Done |
| M3 provenance | Done: message / document + chunk / URL / unresolved / none / corroborated / disputes. Conversation transcript page added as a provenance target |
| M4 entities + temporal | Done: global lens, entity state now / as of, merge redirect, timeline page, "Not time-filtered" labels |
| M5 Graph Explorer | Done: 4 Phase 8 endpoints, read-only, persistent truncation banner, budget-bounded `exclude_ids`, evidence panel, list view, canvas cap |
| M6 ranked search | Done: `POST /search`, text never in the URL |
| M7 documents | Done: upload with size precheck (server authoritative), bounded polling, pipeline states, chunks with anchors. G1 accepted and stated in the UI |
| M8 review / governance | Done: memory duplicates (same / different), conflicts (A replaces B, B replaces A, both true, not a real conflict), entity merge (approve / reject / undo). Consequence dialog, `if_version`, 409 → reload, invalidation after server confirmation only |
| M9 Ask (optional) | Minimal page built: plain-text answer, citations only from `cited_memory_ids`, termination / confidence labels, side effects stated. Not load-tested |
| M10 deployment (optional) | Documented only (ADR 0030, `frontend/README.md`). Nothing deployed |
| G2 | Done (see 29.3) |

### 29.2 M0 findings

Verified with curl and a real Chromium:

* Login through the Next rewrite sets a host-only, HttpOnly,
  `SameSite=Lax` `sb_session` cookie.
* After login, `document.cookie` is empty, and `localStorage` and
  `sessionStorage` are empty.
* Browser POSTs, which carry `Origin` automatically, get 200. A missing or
  foreign `Origin` gets 403.
* Header behaviour of the rewrite:
  * it forwards `Origin` and `Cookie` unchanged;
  * it rewrites `Host` and adds `x-forwarded-host`;
  * it passes a client-supplied `X-Forwarded-For` **verbatim**.

  Consequences:
  * the proxy must not be in `TRUSTED_PROXY_IPS` (ADR 0026, amending
    ADR 0022);
  * CSRF relies on `FRONTEND_URL`, because the backend's same-origin
    comparison never matches through the proxy.
* The rewrite destination is baked into the build output
  (`routes-manifest.json`). `BACKEND_ORIGIN` must be present at build time.
  It does not appear in any client chunk (grep of `.next/static`).

### 29.3 Backend changes (the only ones)

1. **G2: `Cache-Control: no-store`** on every response except `/health*`
   and the docs / OpenAPI paths. It is set centrally in the request
   middleware and in the unhandled-500 handler (`backend/app/main.py`).
   Tests:
   * `tests/unit/test_cache_control.py` (10);
   * one integration test in `tests/integration/test_auth.py`.
2. **Bug fix, pre-existing since `e4b4068`.** `POST /search` returned 500
   for every non-empty result. The agent phase added `graph_distance` /
   `graph_path` to `RetrievalExplanation`, and the route spread all fields
   into the `extra="forbid"` response view. No test called the route with
   a result.
   * Found by the Phase 10 performance run.
   * The route now maps the public fields explicitly; the response
     contract is unchanged.
   * Regression test: `tests/unit/test_search_route.py`, which fails
     without the fix.

No memory-storage, Neo4j-schema, auth or authorization change.
`scripts/e2e_seed.py` is test tooling. It creates and deletes only
throwaway `e2e-*@example.com` accounts and refuses any other state file.

### 29.4 Deviations from the plan

* **No Next.js middleware.** The session gate is a client component around
  the `(app)` layout. The page shell has no user data and the backend
  authorizes every request, so middleware would add only UX (ADR 0026).
* **No shadcn / Radix.** A native `<dialog>` and a handful of owned
  components were enough. Seven runtime dependencies were avoided.
* **ESLint 10 and `eslint-plugin-react`.** The plugin's React version
  detection calls an API that ESLint 10 removed. `settings.react.version`
  is pinned in `eslint.config.mjs` to avoid that code path.
* **Deferred:** a conversations list page (only the transcript exists,
  reachable from provenance and Ask), and manual memory creation (Q10).

### 29.5 Verification (all run on the final tree)

| Check | Result |
|---|---|
| Frontend typecheck (`tsc --noEmit`, strict) | Pass |
| Frontend lint (incl. `react/no-danger` error) | Pass |
| Frontend unit + component tests (Vitest) | **63 passed**, 6 files |
| API contract test (every UI call exists in OpenAPI) | Pass; mutation-tested |
| OpenAPI drift (`gen:api` vs committed schema) | In sync |
| `next build` (production) | Pass, 19 routes |
| E2E (Playwright, production build, strict CSP) | **29 passed**: auth ×7, memory/search ×5, graph, review ×2, isolation, documents ×2, a11y ×10 |
| axe (serious / critical) | 0 on `/login`, `/`, `/memories`, `/entities`, `/search`, `/documents`, `/review`, `/settings`, memory detail |
| Backend full suite (`pytest`, real Neo4j + PostgreSQL) | **1270 passed**, 0 failed |
| Ruff (CI scope `app tests ../scripts`) | Pass |
| Phase 8 conformance (`eval/graph_api_runner.py`) | 39/39 |
| Retrieval evaluation (`eval/retrieval_runner.py`) | **Byte-identical** to `873a409` (72 lines; pristine worktree vs working tree). Mean Recall@K: graph-only 91.7%, vector-only 68.8%, hybrid 100% |
| Document evaluation (`eval/document_runner.py`) | Deterministic section 10/10 PASS |
| Agent evaluation (`eval/agent_runner.py`) | Deterministic baselines unchanged (D_hybrid recall 100%, E_scripted 100/100) |
| `eval/runner.py` | **Fails** with the same pre-existing FK cleanup error documented in Phase 9 (`memory_review_candidates_source_extraction_candidate_id_fkey`). Not caused by and not fixed in Phase 10 |

**Incident (disclosed).** `document_runner.py` and `agent_runner.py`
include a real-LLM section that runs whenever `LLM_PROVIDER` is not
`fake`, and `backend/.env` sets `gemini`. Running them for verification
therefore made real Gemini calls:

* the document runner completed 2 tasks, using 4,591 input and 271 output
  tokens, then hit HTTP 429;
* the agent runner got HTTP 429 on its first call and stopped, recording 0
  tokens.

The 429s are consistent with a free-tier quota, but the billing tier was
not verified. Run these with `LLM_PROVIDER=fake` when no real calls are
intended.

### 29.6 Measured performance (`e2e/perf.spec.ts`, `scripts/bench-graph-layout.mjs`)

| Measurement (budget from section 21) | Result |
|---|---|
| JS, authenticated shell + `/memories`, cold cache (≤ 180 KB gz) | **190.9 KB transferred (gzip)** / 617 KB decoded, 17 requests. **Over budget by ~6%.** Cytoscape not included (verified). `/login` alone, mostly the framework, is 149.3 KB gz |
| Graph chunk (Cytoscape, lazy, `/graph` only) | 430 KB raw / 134 KB gzip |
| LCP, production build, local backend (≤ 2.0 s) | `/memories` authenticated: median 120 ms (n=5); `/login`: median 76 ms |
| Auth bootstrap → first page content | Median 125 ms (n=5) |
| First graph render, 150 nodes / 149 edges at the server budget (≤ 500 ms) | Median 398 ms sync + layout (n=5), excluding paint. Truncation banner shown |
| cose first layout, headless, 150 / 400 / 600 elements | 102 / 686 / 1609 ms median. Only 150 is covered by a budget; see limitations |

API latency through the proxy (median of 5, ms):

| Endpoint | Median ms |
|---|---|
| memory search | 14 |
| memory detail | 12 |
| provenance | 9 |
| graph overview | 49 |
| 2-hop neighbourhood | 16 |
| documents | 9 |
| hybrid search | 47 |

First-call outliers of 90–380 ms were observed (cold plan cache).

### 29.7 Security review

* **Credential handling.** No token in JS or web storage; no
  `Authorization` header from the browser (unit + E2E). The cookie is
  HttpOnly / Lax.
* **Global 401.** It clears the per-user cache. `invalid_credentials` is
  excluded; this fixed a sign-out on a mistyped current password.
* **Open-redirect guard.** Unit-tested vectors plus an E2E off-site `next`.
* **Inert rendering** (ADR 0029):
  * lint rule and CI grep;
  * component and E2E tests with an `<img onerror>` / `<script>` payload;
  * URLs opened only for http(s), with `noopener noreferrer`.
* **Headers.** CSP with `connect-src 'self'`, `frame-ancestors 'none'`,
  `object-src 'none'`; `X-Frame-Options: DENY`; `Referrer-Policy:
  same-origin`; no `X-Powered-By`. `'unsafe-inline'` is required for
  scripts, and `'unsafe-eval'` is dev-only.
* **CSRF.** A foreign `Origin` gets 403 (E2E).
* **Caching.** `Cache-Control: no-store` on authenticated responses,
  asserted through the proxy (E2E).
* **Isolation.** User B gets 404 for user A's memory, entity and review
  item, and sees 0 memories (E2E). No client-controlled `user_id` exists
  anywhere in the UI.
* **Build secrecy.** `BACKEND_ORIGIN` absent from client chunks.
  `.env*` ignored except `.env.example`. The E2E state file lives in
  gitignored `e2e/.auth/` with mode 0600.

### 29.8 Known limitations

1. The JS budget for the authenticated memories route is missed by ~6%.
2. A full re-layout near the 600-element canvas cap blocks the main thread
   for ~1.6 s. A web-worker layout is deferred.
3. Per-IP login limiting is a shared bucket behind the proxy (G4
   deferred). Per-email and per-user limits work.
4. G1: a document page cannot list the memories derived from it (no
   backend endpoint, accepted in Q3).
5. The CSP allows inline scripts. A nonce-based CSP would need dynamic
   rendering and is deferred.
6. Dates are shown in the browser's local time zone. The lens converts
   local input to a UTC instant.
7. The Ask page has no streaming, and each turn can take up to 90 s with a
   real LLM.
8. A conversation list page and manual memory creation are deferred.
9. Nothing is deployed. The free-tier topology (ADR 0030) has cold starts,
   ephemeral document bytes and AuraDB auto-pause. The system is **not**
   production-ready: no load testing, backups or monitoring.
10. `next dev` did not pick up route directories created while it was
    running; a restart fixed it. This is a development-only observation.
