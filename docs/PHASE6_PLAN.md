# Phase 6 Plan — Document Ingestion

**Status:** Approved, with one additional hard project-wide constraint added at
approval time (below). Implementation proceeding.

Design only when first written, per the approved process. This plan follows
the same discipline as Phases 2–5: reuse existing components, add the
smallest new surface that closes a real gap, and never introduce
infrastructure without a demonstrated need.

## Zero-cost constraint (added at approval)

**The entire project must remain buildable, testable, and demonstrable
without paying money, at every phase, including this one.** No paid API,
paid cloud resource, paid storage service, paid database tier, or
billing-enabled service may be *required*. Free/open-source software, local
services, and genuine free tiers (Gemini's free tier, as already used since
Phase 2) are the only external dependencies allowed. If a free-tier quota is
exhausted during development or evaluation, the response is: report the
limitation honestly, fall back to a free/local alternative where reasonable,
and never enable billing or upgrade a tier to work around it.

**Concrete consequence for this plan:** section 10's storage design is
revised. Phase 6 v1 ships **`LocalDocumentStorage`** (the local filesystem)
as its only real backend — not an S3-compatible one. `aioboto3` is **removed**
from section 25's dependency list; it is not added "for future use," since
nothing in v1 actually needs it, and every paid S3-compatible provider
(Cloudflare R2, Supabase Storage, AWS S3) would require a billing account to
verify against, which the zero-cost constraint forbids doing now. The
`DocumentStorage` Protocol (unchanged in spirit from section 10's original
`StorageClient` design) still makes a future S3-compatible implementation
possible without changing any caller — but that implementation is not built
until a concrete, non-speculative need exists. Everywhere below that
originally said `StorageClient`/`FakeStorageClient`/an S3 adapter, read
`DocumentStorage`/`LocalDocumentStorage` instead; the rest of the plan
(sections 0–9, 11–26) is otherwise unchanged by this revision.

---

## 0. What Phase 6 extends, not replaces

```text
message → PostgreSQL → gate → window → job → LLM → grounding → candidate
    → decision engine (exact-triple, then semantic evidence) → Neo4j → provenance
```

Phase 6 adds a **second producer** into the exact same pipeline, entering
after "window":

```text
document → ingestion job → parse → chunk → [same] job → [same] LLM
    → [same] grounding → [same] candidate → [same] decision engine
    → [same] Neo4j → [same] provenance → [same] embeddings
    → [same] hybrid retrieval → [same] agent
```

**The governing conceptual requirement, restated as the design constraint it
actually is:** a document chunk plays the same role in this pipeline a
conversation message already plays. It is not a new kind of memory, not a
new decision engine, not a new retrieval system, and not a new agent
capability. Concretely, after inspecting the repository, the pipeline stages
that need **zero code change** are: the decision engine
(`app/extraction/decisions.py`), grounding (`app/extraction/grounding.py` —
its signature already takes plain strings, not a `Message`), entity
resolution (`app/extraction/resolution.py`), the embedding pipeline
(`app/semantic/embedding_pipeline.py`), `HybridRetrievalService`, every
agent tool except `get_source` (which needs a narrow, additive extension),
and every existing Neo4j node/relationship type. The stages that need
**additive, narrow** changes are: `ExtractionJob`/`ExtractionCandidate`
(widened to accept a chunk as a target, alongside a message), a new
ingestion-side pipeline (parse → chunk, with its own job table mirroring
`extraction_jobs`'s exact shape), a new object-storage layer (genuinely new
— Phase 1–5 never stored a file), and a new document-specific extraction
prompt (same schema, `ExtractionResult`, different framing text).

**Two findings from inspection that materially simplify this plan:**

1. `SourceType.DOCUMENT` and `EntityType.DOCUMENT` **already exist** in
   `app/models/enums.py`, unused since Phase 1 — `CLAUDE.md` section 16
   listed "document" as a source kind from the very first commit. Using them
   is not a schema change; it is the first real use of an already-provisioned
   value.
2. `.env.example` already carries unused `STORAGE_ENDPOINT` /
   `STORAGE_BUCKET` / `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` (and
   `REDIS_URL`, addressed in section 8) — placeholders present since before
   Phase 1 shipped, exactly like `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL` sat
   unused until Phase 3 filled them in. Phase 6 is what fills these in.

**Kept unchanged, explicitly:** Neo4j as sole canonical memory (ADR 0006);
the LLM-proposes/application-decides boundary and conservative policy (ADR
0007); the exact-triple-first, semantic-evidence-second decision engine (ADR
0009); the bi-temporal, append-only memory model (ADR 0002); the review
workflow and its state machine (ADR 0010); the agent's tool-calling loop, its
citation-validation mechanism, and `propose_memory_update`'s reuse of
`ExtractionRunner._apply_candidate` (ADR 0011); `FakeLLMClient`/
`FakeEmbeddingClient` as the default, no-API-key test posture; Railway/
Vercel/Aura, no Docker, no Kubernetes.

---

## 1. Supported document types for Phase 6 v1

| Format | v1? | Why |
|---|---|---|
| **TXT** | Yes | Zero new dependency — it is already a string. The cheapest possible way to prove the whole pipeline end to end before spending effort on a real parser. |
| **Markdown (.md)** | Yes | Same parsing cost as TXT (no rendering needed, only heading detection via a small regex for chunk metadata, section 6) — effectively free once TXT works. |
| **PDF (text layer only)** | Yes | The format `PRODUCT.md` section 11 names first, and the one users actually mean by "document ingestion." Requires exactly one new, narrow dependency (section 25). Scanned/image-only PDFs are explicitly **out of scope** (section 5) — detected and rejected, never silently mishandled. |
| **DOCX** | **No — deferred** | Requires a *second*, structurally different parsing library (`python-docx`, a different API than PDF text extraction). Per "avoid dependency accumulation" and "do not automatically support every format": ship TXT+MD+PDF first, prove the harder parts of the pipeline (chunking, page-aware provenance, header/footer handling) against real text-heavy formats, then add DOCX as a narrow, low-risk follow-up once the pipeline is proven. Nothing about DOCX is architecturally interesting — it is exactly one more parser behind the same `DocumentParser` protocol (section 25). |

No other format is considered for v1 (no HTML, no EPUB, no images). Section
20 restates this as a scope boundary.

---

## 2. Upload and ingestion lifecycle

Two lifecycles at two granularities, mirroring the existing
`messages.extraction_status` (coarse) / `extraction_jobs.status` (fine)
split exactly:

```text
Document.status (PostgreSQL, coarse — one row per document):

  UPLOADED
      │
      ▼
  VALIDATING        mime/size/page-count checks (section 8)
      │  reject ──────────────────────────────────────────► FAILED
      ▼
  PARSING           text extraction (section 5)
      │  malformed/scanned/empty ─────────────────────────► FAILED
      ▼
  CHUNKING          deterministic split into document_chunks rows (section 6)
      │
      ▼
  CHUNKED           chunks exist; ready for extraction
      │
      ▼
  EXTRACTING        ≥1 chunk's extraction_job is pending/in_progress
      │
      ├─ every chunk reached a terminal state, all succeeded ──► COMPLETE
      └─ every chunk reached a terminal state, some failed ───► PARTIALLY_COMPLETE
```

`IngestionJob.status` (PostgreSQL, fine — one row per document, owns
`VALIDATING`→`PARSING`→`CHUNKING` only): `pending` → `in_progress` → `done` |
`failed` | `abandoned` — **the exact same shape as `ExtractionJob`**:
`locked_at`/`locked_by` lease, `attempts`, `next_attempt_at` backoff,
`error_code`/`error_detail`. Claimed with the identical
`SELECT ... FOR UPDATE SKIP LOCKED` pattern `ExtractionQueue` already uses.

`DocumentChunk.extraction_status` (PostgreSQL, one row per chunk): `pending`
→ `queued` → `in_progress` → `done` | `failed` | `skipped` — **the exact
same values `messages.extraction_status` already uses**, because a chunk's
relationship to its own `ExtractionJob` is structurally identical to a
message's.

**Failure states:** a document that fails `VALIDATING`/`PARSING`/`CHUNKING`
never produces chunks and sits in `FAILED` with `error_code`/`error_detail`
(mirrors `ExtractionJob`'s existing fields) — e.g. `unsupported_mime`,
`file_too_large`, `too_many_pages`, `malformed_pdf`, `no_extractable_text`.

**Retry behavior:** identical to extraction's three layers (docs/PHASE2_PLAN.md
section 7) — job identity (`IngestionJob` claim/lease/backoff), effect
identity (chunk `content_hash`, so a re-run of chunking against unchanged
parsed text produces byte-identical chunks), and "the pipeline itself is
idempotent" (a chunk already `done` is skipped, not re-extracted).

**Idempotency:** a document-level `content_hash` (sha256 of the raw uploaded
bytes) with a `UNIQUE (user_id, content_hash)` constraint — re-uploading the
identical file returns the existing `Document` row rather than creating a
second one. This is new (nothing today needs whole-file dedup), but it is
the same idiom as `MemoryEmbedding`'s `content_hash` guard, applied one
level up.

**Cancellation:** **not implemented in v1.** No existing job in this
codebase (extraction, embedding backfill, review reconciliation) supports
user-initiated cancellation either — adding it here would be new complexity
with no established precedent to extend, and nothing in the brief asks for
it. Named as a deferred feature (section 20).

**Partial failure behavior:** a single chunk's parse-adjacent or
extraction failure never fails the whole document — mirrors extraction's
own "one bad candidate does not abort the job" discipline exactly.
`PARTIALLY_COMPLETE` is a real, first-class, queryable outcome, not an
error state: the document is usable (its successfully-extracted memories
are already in Neo4j and retrievable) while a bounded sweep (mirroring the
extraction sweep's "gate-mistake re-evaluation" case) periodically retries
chunks still `failed` with attempts remaining.

**Trigger:** `BackgroundTasks` registered after the upload endpoint's
response is returned — the user never waits for parsing, chunking, or
extraction, exactly mirroring how `POST /conversations/{id}/messages`
schedules extraction only after committing the message. **No new queue
infrastructure.**

---

## 3. Document data model

### New PostgreSQL tables

```python
class Document(Base):
    """One uploaded file. Never holds the file's bytes -- storage_key is an
    opaque pointer into object storage (section 10), the same discipline
    ExtractionCandidate.memory_id already uses for Neo4j.
    """
    __tablename__ = "documents"

    id: str                          # uuid, pk
    user_id: str                     # FK users.id
    filename: str                    # display-only, sanitized, never used to
                                      # build a path (section 9)
    content_type: str                # sniffed MIME, not trusted from the client
    size_bytes: int
    content_hash: str                # sha256 of raw bytes -- whole-file dedup
    storage_key: str                 # opaque pointer, object storage (section 10)
    page_count: int | None           # PDF only
    status: str                      # section 2's coarse lifecycle
    error_code: str | None
    error_detail: str | None
    uploaded_at: datetime
    processed_at: datetime | None

    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="documents_user_dedup"),
        CheckConstraint("status IN ('uploaded','validating','parsing','chunking',
                          'chunked','extracting','complete','partially_complete',
                          'failed')"),
        Index("documents_user_status", "user_id", "status"),
    )


class DocumentChunk(Base):
    """One deterministic slice of a document's parsed text -- the document
    analogue of a Message. Content lives here (small, queryable, needed for
    grounding/provenance), never the original file bytes.
    """
    __tablename__ = "document_chunks"

    id: str                          # uuid, pk
    document_id: str                 # FK documents.id, ON DELETE CASCADE
    user_id: str                     # FK users.id (denormalized, matches
                                      # every other table's isolation column)
    ordinal: int                     # position within the document
    content: str                     # extracted chunk text
    content_hash: str                # sha256 of `content`
    page_start: int | None           # PDF only
    page_end: int | None             # PDF only
    heading_context: str | None      # e.g. "Intro > Background" (Markdown/PDF)
    char_start: int                  # offset into the document's full parsed
    char_end: int                    # text -- the document-level analogue of
                                      # CandidateMemory.source_span
    extraction_status: str           # section 2's chunk lifecycle
    created_at: datetime

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="document_chunks_ordinal"),
        Index("document_chunks_document_order", "document_id", "ordinal"),
        Index("document_chunks_extraction_todo", "extraction_status",
              postgresql_where=text(
                  "extraction_status IN ('pending','failed','skipped')")),
    )


class IngestionJob(Base):
    """VALIDATING -> PARSING -> CHUNKING only -- the exact claim/lease/retry
    shape as ExtractionJob, one row per document.
    """
    __tablename__ = "ingestion_jobs"

    id: str
    user_id: str
    document_id: str                 # FK documents.id, UNIQUE (one job per doc)
    status: str                      # pending | in_progress | done | failed | abandoned
    attempts: int
    locked_at: datetime | None
    locked_by: str | None
    next_attempt_at: datetime | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None

    __table_args__ = (
        UniqueConstraint("document_id", name="ingestion_jobs_one_per_document"),
        CheckConstraint("status IN ('pending','in_progress','done','failed','abandoned')"),
        Index("ingestion_jobs_claimable", "status", "next_attempt_at"),
    )
```

### Widened, not duplicated: `ExtractionJob` and `ExtractionCandidate`

This is the single most important integration decision in this plan (ADR
0012, section 22). `ExtractionJob.target_message_id` and
`ExtractionCandidate.source_message_id` are today `NOT NULL` foreign keys to
`messages.id`. A document chunk needs to be a target/source too, without
creating a synthetic `messages` row (which would blur "a conversation turn"
with "a document excerpt" — two genuinely different things) and without a
parallel `document_extraction_jobs` table (which would fork the claim/lease/
retry/idempotency machinery `ExtractionRunner`/`ExtractionQueue` already
implement correctly).

```python
# ExtractionJob — additive columns only, existing rows unaffected
source_kind: str = "message"          # 'message' | 'document_chunk' (NEW, NOT NULL,
                                       # server_default='message' backfills every
                                       # existing row for free)
target_message_id: str | None         # now NULLABLE (was NOT NULL)
target_chunk_id: str | None           # NEW, FK document_chunks.id, NULLABLE
# CHECK: exactly one of target_message_id / target_chunk_id is set, matching source_kind

# ExtractionCandidate — same treatment
source_message_id: str | None         # now NULLABLE
source_chunk_id: str | None           # NEW, FK document_chunks.id, NULLABLE
# CHECK: exactly one of source_message_id / source_chunk_id is set
```

`window_start_sequence`/`window_end_sequence` are **reused unchanged** for
document jobs: a chunk's `ordinal` plays the exact role a message's
`sequence` plays today, so "window" becomes "a small range of adjacent
chunk ordinals" with zero new columns. `extractor_version` gains a
`document:` prefix component (mirroring the `agent:` prefix Phase 5 already
established for agent-authored jobs), so a document job's idempotency key
can never collide with a conversation job's, or with another document job
under a different extraction prompt version.

### Neo4j — no schema change

Zero new node labels, zero new relationship types, zero new properties.
`Source.source_type = SourceType.DOCUMENT`, `Source.reference =
<document_id>`, `Source.location = <chunk_id>` (or `"page:3"` when no chunk
id is meaningful) is exactly the shape `Source` has had since Phase 1 — this
phase is the first time `source_type=DOCUMENT` is actually written. A
document-derived `Memory` is created via the **same** `MemoryService.create`
every other memory uses, connects to `Entity` nodes via the **same**
`RELATES_TO` edges, and is embedded into the **same** `memory_embeddings`
table (section 11). There is no `DocumentMemory` type and no parallel
storage path.

### Relationship to existing `Source`/`Memory`/`Entity`/`Embedding`

Unchanged in every dimension except one new, valid `source_type`. A
document-derived memory is, from the decision engine's and the agent's
point of view, indistinguishable from a conversation-derived one — which is
exactly the point (section 0's governing constraint). "Avoid duplicating
canonical memory" is satisfied structurally: nothing about a document's
content is ever written into Neo4j except the LLM's distilled
`CandidateMemory.statement`, exactly as conversation extraction already
works.

---

## 4. Provenance

```text
Memory
  │  Source.source_type == DOCUMENT
  ▼
Source (Neo4j: reference=<document_id>, location=<chunk_id>)
  │  opaque-id lookup, PostgreSQL
  ▼
Document (filename, uploaded_at) + DocumentChunk (content, page_start/end,
                                                    heading_context)
```

Every memory extracted from a document is traceable to: document
(`documents.filename`/`uploaded_at`), chunk (`document_chunks.id`/`content`/
`heading_context`), page number where available (`page_start`/`page_end`,
`None` for TXT/MD), and extraction/model information (the owning
`extraction_jobs` row already carries `provider`/`model`/`prompt_version`/
`input_tokens`/`output_tokens`/`latency_ms` — unchanged fields, now also
populated for document jobs).

**`get_source` is extended, not replaced.** `ProvenanceService.get_provenance`
already resolves a message-shaped `Source.reference` into a `Message`
(`SourceProvenance.message: Message | None`); it gains a second, parallel
resolution branch for `source_type == DOCUMENT`, adding
`SourceProvenance.document: DocumentSummary | None` and
`SourceProvenance.chunk: DocumentChunkSummary | None` (both `None` for every
existing, non-document source — **purely additive**, so no existing test
asserting message-shaped provenance responses needs to change).
`app/agent/tools.py`'s `get_source` tool function needs a small rendering
change (include document/chunk/page fields in its compact output when
present) — **no new tool**.

"Where did you get this information?" then works exactly as it does for
conversations today: the agent calls `search_memory` (finds the memory,
regardless of source), then `get_source(memory_id)` (returns document
filename + chunk text + page number when the source is a document), and
answers in prose with page-level detail. Nothing about the agent's tool
selection needs to change to support this — it already asks `get_source`
for provenance detail when the user asks a provenance-shaped question
(demonstrated live in Phase 5's real-LLM `provenance` evaluation task).

Structural citation validation (`cited_memory_ids` checked against every
memory id a tool call actually returned this turn, docs/PHASE5_PLAN.md
section 9) is **completely unaffected** — a document-derived memory has a
`memory_id` exactly like any other, so it participates in citation
validation with zero special-casing (section 14 expands on this).

---

## 5. PDF handling

| Concern | v1 decision |
|---|---|
| Text-based PDFs | Supported — the only PDF case v1 targets. |
| Scanned PDFs (image-only) | **Not supported.** Detected after parsing: if extracted text density falls below a threshold (`MIN_EXTRACTABLE_TEXT_RATIO`, e.g. extracted characters per page below a small floor), the document fails `PARSING` with `error_code="no_extractable_text"` — never silently produces near-empty chunks. |
| Page boundaries | Preserved as `page_start`/`page_end` chunk metadata (section 3) — used for provenance (section 4), not as a hard chunk-splitting rule (a chunk may span a short page rather than fragment into many tiny chunks). |
| OCR | **Not introduced.** No Tesseract, no cloud OCR API, no new infrastructure category — a scanned PDF is a clear, honest failure, not a silent degradation. Named explicitly in section 20 as deferred. |
| Tables | Extracted as whatever plain text the parsing library linearizes them to (no structured table→JSON system). A stated limitation, not a defect — table understanding is real, separate scope (section 20). |
| Headers/footers/repeated text | **Mitigated, cheaply.** A real, measurable extraction-quality risk (a running header/footer repeated on every page can itself get "extracted" as a fact). v1 heuristic: after parsing all pages, any line that repeats verbatim across a majority of pages (`HEADER_FOOTER_REPETITION_THRESHOLD`, e.g. >50%) is stripped before chunking — a bounded, deterministic, exhaustively unit-testable function, not an ML classifier. Cheap enough to include now rather than defer. |
| Malformed PDFs | Parsing wrapped in the same "never let one bad job crash a worker tick" discipline as every existing job processor — a parse exception maps to `FAILED` with `error_detail` from the library's own exception, never propagates. |
| Very large PDFs | Bounded by `MAX_DOCUMENT_PAGES` (section 8), rejected at `VALIDATING`, never silently truncated. |

---

## 6. Chunking strategy

**Deterministic, fixed-size, character-based — not semantic.** No
embedding-based boundary detection, no ML segmentation model.

```text
CHUNK_SIZE_CHARS (Settings, proposed default 2000)
CHUNK_OVERLAP_CHARS (Settings, proposed default 200)
```

Same "tunable `Settings` field, not a magic number" convention as
`EXTRACTION_WINDOW_SIZE`. The parsed document's full text is split into
fixed-size, overlapping windows; the boundary snaps backward to the nearest
paragraph break (or, failing that, sentence break) before the hard
character limit where one exists within a small lookback — a simple,
deterministic rule ("prefer not to split mid-sentence"), not full NLP
sentence segmentation.

**Page boundaries:** recorded as metadata (`page_start`/`page_end`,
computed from the character-offset-to-page mapping the PDF parser already
provides), not enforced as a hard split — a chunk may legitimately span two
short pages.

**Preserving headings:** for Markdown (and PDF, where headings are
detectable via font-size/style heuristics the parsing library exposes), the
current heading path (`"Intro > Background"`) is tracked via simple regex/
structural detection and attached as `heading_context` per chunk — cheap,
deterministic, valuable for citations ("chunk 7, under 'Background'"), not
a semantic-understanding feature.

**Chunk IDs:** deterministic UUIDs generated at creation time (the same
`id_fn` convention used everywhere else in this codebase), not
content-hash-based — ordinal position matters for windowing (section 7), so
a pure content hash would lose that. `content_hash` is still stored per
chunk (section 3) as an idempotency/resumability guard, mirroring
`MemoryEmbedding.content_hash`'s exact stated purpose: if re-parsing an
unchanged document ever produced a different hash for the same ordinal,
that is a signal something upstream changed unexpectedly, worth flagging,
not silently trusting.

**Metadata per chunk (recap):** `ordinal`, `page_start`/`page_end`,
`heading_context`, `char_start`/`char_end` — the last pair is what makes
exact-location provenance possible, the document-level analogue of
`CandidateMemory.source_span`.

**Avoid premature semantic chunking:** explicitly deferred. If evaluation
(section 15) shows fixed-size chunking measurably hurts retrieval or
extraction quality, that becomes a documented, evidence-based v2 candidate
— not a default assumption now, per the same discipline Phase 3 applied to
the ranking formula and Phase 4 applied to fan-out capping.

---

## 7. Extraction strategy

Reuses `LLMClient.complete_structured(schema=ExtractionResult)`,
`CandidateMemory`, `check_grounding`, and the entire decision engine —
**unchanged code**. What differs, and why:

| Aspect | Conversation extraction | Document extraction |
|---|---|---|
| Trigger | Deterministic gate (`app/extraction/gate.py`) — messages are extraction-*candidates*, most are filtered out | **No conversational gate.** A user explicitly uploaded this document to learn from it; a chunk is extraction-worthy by default. A narrow, separate heuristic only skips chunks that are pure whitespace or clearly non-prose (e.g. a bare table of contents) — not a reuse of `evaluate_gate`, whose heuristics (declarative-not-a-question, persistence patterns) are conversational and do not transfer. |
| Window | Sliding window of recent conversation messages, for pronoun/context resolution (ADR 0008) | A chunk plus its immediately adjacent chunks (prev/next ordinal), for context across a chunk boundary that split an idea mid-thought — same *purpose* (give grounding something to resolve against), different *source* of adjacency. |
| Prompt | `app/llm/prompts/extraction_v1.py` | **New**, `app/llm/prompts/document_extraction_v1.py`, mirroring that module's exact shape — different framing ("the following document excerpt states...", no "the user said" framing, no role concept), producing the **identical** `ExtractionResult`/`CandidateMemory` schema, so the decision engine downstream needs zero changes. |
| Grounding | `check_grounding(candidate, target_message_content=..., window_text=...)` | **Same function, same signature** — it already takes plain strings, never a typed `Message`. Callers pass chunk text instead of message text; nothing in `app/extraction/grounding.py` changes. |
| `role` concept | user / assistant / system | Does not apply — every document chunk is always "the document says X," never attributed to a conversational speaker. |
| Duplicate/conflict detection | Exact-triple then semantic evidence, cross-conversation already | **Unchanged, and now genuinely cross-source for the first time** — a document-asserted fact duplicating or conflicting with a conversation-asserted one (or another document's) is caught by the same decision engine with zero document-specific logic. This is a new *test surface* (section 17/24), not new *code*. |
| Entity resolution | `app/extraction/resolution.py` | Unchanged. |
| Temporal information | `CandidateTemporal` hints (`valid_from_hint`, `is_change`, `replaces_hint`) | Unchanged — a document asserting "as of March 2026, ..." is handled by the same temporal decision logic already built for conversational date hints. |
| Provenance | `source_message_id` | `source_chunk_id` (section 3's widened schema). |

One `ExtractionJob` per chunk (not per document) — small, resumable,
independently retryable, consistent with "bounded work units" everywhere
else in this codebase (a crash mid-document loses at most one in-flight
chunk's progress, section 8). Document-level context (title/filename) is
passed into the prompt as fixed context, never treated as part of the
"window."

---

## 8. Large-document processing

| Bound | Setting | Proposed default | Enforced |
|---|---|---|---|
| Max file size | `MAX_DOCUMENT_SIZE_BYTES` | 20 MB | `VALIDATING`, before parsing |
| Max pages (PDF) | `MAX_DOCUMENT_PAGES` | 200 | `VALIDATING`, before parsing |
| Max chunks per document | `MAX_CHUNKS_PER_DOCUMENT` | 500 | `CHUNKING`, a safety net even if size/page limits are somehow bypassed |
| Parse timeout | `INGESTION_PARSE_TIMEOUT_SECONDS` | 60 | Ingestion job's parse step — a malformed/huge PDF must not hang a worker forever |
| Batch size (ingestion) | `INGESTION_SWEEP_BATCH_SIZE` | 20 | Mirrors `EXTRACTION_SWEEP_BATCH_SIZE`'s exact role |
| Batch size (extraction) | *(existing, unchanged)* `EXTRACTION_SWEEP_BATCH_SIZE` | — | Chunk jobs are ordinary rows in the same `extraction_jobs` queue; no new limit needed |
| Cost | *(existing, unchanged)* `EXTRACTION_DAILY_TOKEN_BUDGET` | — | Already a placeholder setting (Phase 2); extended to cover document jobs too, since they use the same `LLMClient` and the same budget concept — not a separate document-specific budget |

**Background processing:** `BackgroundTasks` + the existing sweep pattern.
**No Redis, no Celery, no Kafka, no new queue infrastructure** — the same
conclusion Phase 2 reached for extraction applies with equal force here:
nothing about document volume in this project's stated scale (a single
user's uploaded documents, not a bulk ingestion service) demonstrates a need
the existing bounded-sweep pattern cannot satisfy. `REDIS_URL`'s presence in
`.env.example` remains an unused placeholder after this phase too — named
explicitly so it is not mistaken for an oversight.

**Resumability:** two independent levels. Parsing/chunking is resumable at
the `IngestionJob` level (lease/claim/retry, identical to `ExtractionJob`).
Extraction is resumable at the *chunk* level — each chunk is its own
`ExtractionJob`, so a crash mid-document loses at most one in-flight chunk's
progress, never the whole document's.

**Idempotency:** document-level `content_hash` dedup (section 2) stops
re-parsing an identical re-upload entirely; chunk-level `content_hash` plus
the existing 3-layer extraction idempotency (job identity, candidate-hash
effect identity, the decision engine's own idempotent-by-construction
behavior) covers everything downstream.

**Rate limits / cost controls:** the existing `EXTRACTION_DAILY_TOKEN_BUDGET`
mechanism, extended to include document extraction jobs in its accounting —
one budget, one LLM, one accounting mechanism, not two.

---

## 9. Security

| Concern | Mitigation |
|---|---|
| File type validation | Content-sniffed MIME (magic bytes), never the client-supplied filename extension alone. Reject anything outside the whitelisted set (section 1). |
| MIME validation | Same point; enforced at `VALIDATING`, before any parsing library ever sees the bytes. |
| Size limits | Section 8. |
| Malicious/malformed files | Parsing runs under a timeout (section 8) and the same "never let one bad job crash a worker" try/except discipline as every existing job processor. **Not** process-level sandboxing of the parser (a genuine, named v1 limitation — real infrastructure this plan does not add without evidence of need). |
| Prompt injection inside documents | **The exact same containment ADR 0007 already established.** Document text is extraction input, never fed back into a prompt as an instruction; grounding still requires a claimed entity mention to literally exist in the chunk text. Once a document-derived memory is retrieved by the agent, it flows through the same `render_tool_result()`/prompt structure Phase 5's real-LLM evaluation already validated keeps retrieved content in the "Tool results" section, never merged into system instructions — no new containment mechanism is needed on the agent side. |
| Treating document content as untrusted DATA | Structural, inherited, not re-implemented — same point as above. |
| User isolation | `documents`/`document_chunks`/`ingestion_jobs` are `UserScopedRepository` subclasses exactly like every other table — covered by the existing SQL-safety AST audit (`tests/unit/test_sql_safety.py`) with **zero changes to that audit's scope rules**, since it already walks every file under `app/repositories/`. |
| Filename sanitization | The original filename is stored as **display-only** metadata, never used to construct a storage path. |
| Path traversal | Structurally eliminated by the point above — the storage key (section 10) is a generated identifier, never derived from user input, so an untrusted filename never touches a path at all. |
| Secret leakage | Document content is never logged (same CLAUDE.md section 32 discipline already followed everywhere); `STORAGE_ACCESS_KEY`/`STORAGE_SECRET_KEY` live in env-only secrets, never logged, never returned in any API response. |
| API abuse / rate limiting | Named as a Phase 8 concern, consistent with every prior phase — no individual phase has added per-endpoint rate limiting yet, and this one does not either. |

**The single most important invariant of this phase, restated plainly: a
document must never be able to inject instructions into the agent.** It
inherits this guarantee from ADR 0007 and from Phase 5's already-validated
agent-side containment; it does not need, and does not get, a new
mechanism. The regression test is a direct port of Phase 5's real, validated
`prompt_injection` evaluation task (section 15/17/24) — a document
containing an embedded instruction, extracted, retrieved, and confirmed to
be treated as quoted content, not a command.

---

## 10. Storage (revised at approval — zero-cost constraint)

**Document bytes never live in PostgreSQL or Neo4j.** A multi-megabyte
binary blob in a `BYTEA`/`TEXT` column is the wrong tool — it bloats
backups and connection-pool memory for no benefit `documents` doesn't
already get from a pointer. This mirrors ADR 0006's own boundary rule
(PostgreSQL for application state, never a copy of the "real" content)
applied to a genuinely new kind of content.

**v1 ships exactly one real backend: the local filesystem.** No paid
object-storage account is required to build, test, or demonstrate Phase 6.

```text
app/storage/base.py             DocumentStorage Protocol + StorageResult --
                                 structurally identical in shape to
                                 LLMClient/EmbeddingClient (put/get/delete by
                                 opaque key)
app/storage/local.py            LocalDocumentStorage -- writes under a
                                 configured root directory
                                 (DOCUMENT_STORAGE_DIR), one file per
                                 document, keyed by a generated id, never by
                                 the untrusted filename (section 9)
app/storage/factory.py          build_document_storage()/get_document_storage(),
                                 mirroring app/llm/factory.py exactly
```

**No `FakeDocumentStorage` needed as a separate class.**
`LocalDocumentStorage` pointed at a `pytest`-managed temp directory *is*
the test double — deterministic, no network, no cost, and it exercises the
real filesystem code path rather than an in-memory stand-in, which is
strictly more honest for the one thing this abstraction actually has to get
right (safe key generation, no path traversal). This differs from
`FakeLLMClient`/`FakeEmbeddingClient`, which fake out a genuinely remote,
non-free API; there is nothing to fake here that isn't already free and
local.

**No S3-compatible adapter is built in v1.** The `DocumentStorage` Protocol
is intentionally shaped so a future `S3CompatibleDocumentStorage` could be
added without changing any caller, but it is not implemented now: every
realistic S3-compatible provider (Cloudflare R2, Supabase Storage, AWS S3)
requires a billing-capable account to even verify against, which the
zero-cost constraint rules out doing speculatively. `aioboto3` is **not** a
Phase 6 v1 dependency. If a future phase has a concrete, demonstrated need
for off-box storage (e.g. a real multi-instance deployment where local disk
isn't shared), that is the point to add it — not before.

**PostgreSQL holds:** `documents.storage_key` (opaque pointer, exactly like
`ExtractionCandidate.memory_id` — here, the generated on-disk filename, not
a path), filename (display-only), content type, size, checksum, status,
timestamps — **never the bytes**. `document_chunks.content` (the *extracted
text*, not the original file) **is** stored in PostgreSQL, mirroring
`messages.content` exactly — small, queryable, needed for grounding and
provenance display; only the original uploaded bytes are large enough, and
opaque enough, to belong outside PostgreSQL.

---

## 11. Embeddings

Reuses `EmbeddingClient`/pgvector **unchanged** — no new embedding
abstraction, no new provider adapter.

- **Extracted memories:** embedded exactly like any other memory, via the
  existing `embed_and_store_memory` → `memory_embeddings` path, triggered
  the same way (a `BackgroundTask` after the Neo4j write). This is what
  makes document-derived facts appear in the same hybrid retrieval as
  conversation-derived facts automatically, with no document-specific code.
- **Document chunks:** **not embedded in v1.** Considered and explicitly
  deferred (section 12 explains why), because `get_source`'s exact-pointer
  lookup (section 4) already answers "show me where this came from" without
  needing a chunk-level similarity index at all — provenance is an exact
  lookup by `Source.reference`, never a search.

**Both need embeddings only if raw-chunk retrieval is added later** (an
explicit, deferred v2 direction, section 20) — v1 needs exactly one
embedding path, the one that already exists.

---

## 12. Retrieval architecture

**A. extracted memories only** is the v1 answer, for both retrieval and the
agent's default tool (`search_memory`).

| Option | What it retrieves | v1? |
|---|---|---|
| A. Extracted memories only | Distilled, structured `Memory` nodes — the existing `HybridRetrievalService` path, unchanged | **Yes** |
| B. Raw document chunks | Chunk text via a chunk-level vector index | No — deferred |
| C. Both, merged | A candidate set mixing memory nodes and raw chunks | No |

**Why A, not C:** the brief's own framing is the deciding argument —
*memory is canonical semantic knowledge; raw document chunks are provenance/
supporting evidence.* Merging chunk-level candidates into the same ranked
result set `HybridRetrievalService` returns would blur exactly that
distinction, and would require `rank_score`/`RetrievalExplanation`/citation
validation to understand two structurally different kinds of "thing found"
— a real redesign of code that has already been built, tested, and (for the
agent) real-LLM validated. A is the option that requires **zero changes**
to `HybridRetrievalService`, `rank_score`, `RetrievalExplanation`, or any
agent tool's retrieval path: a document-derived memory is retrieved via the
exact same vector+graph+keyword+temporal pipeline every other memory uses.

**The accepted tradeoff, stated plainly:** a fact stated only implicitly or
verbosely in a document chunk, but not cleanly distilled by extraction into
a `CandidateMemory`, is not retrievable in v1 — the same limitation
"extraction, not raw-text search" has always had for conversations, already
accepted there. If evaluation (section 15/16, specifically Baseline B) shows
real facts are being missed this way at a rate that matters, raw chunk
retrieval becomes a documented, evidence-based v2 candidate — never a
default assumption now.

---

## 13. Agent integration

**The existing six read tools are sufficient.** `search_memory` already
surfaces document-derived memories once embedded (section 11) — they are
indistinguishable from conversation-derived ones at the `Memory` node
level, so no tool needs to know a memory's origin to retrieve it correctly.

**`get_source` needs the section 4 extension** (resolve a `DOCUMENT`-typed
source into filename/chunk/page) — a service-layer and schema change, not a
new tool.

**A new tool was considered and rejected:** a hypothetical `list_documents`
tool (so the agent could answer "what documents do I have?"). Not included
in v1 — none of the required evaluation task categories (section 15) need
it, and "what documents exist" is arguably better served by a future
document-explorer UI than an agent tool call. Named explicitly as a
deferred, not a forgotten, option (section 20) — "do not add tools just
because they are technically possible" is honored directly.

**`propose_memory_update` is unchanged, not extended.** It is the agent
proposing a memory from the live *conversation*; document extraction is a
separate, passive pipeline triggered by upload, not something the agent
does. The two interact only by funneling into the same decision engine —
exactly the reuse this whole plan is built around, with no new coupling
between them.

---

## 14. Provenance-aware citations

The final agent answer's `cited_memory_ids` mechanism — validated
structurally against every memory id any tool call actually returned this
turn (docs/PHASE5_PLAN.md section 9) — is **completely unchanged**. It still
only ever holds memory ids, regardless of a memory's source.

The `Memory → Document → Page → Chunk` chain is exposed as *result data*
from `get_source` (section 4), which the agent can then reference in its
own prose ("According to `report.pdf`, page 3: ..."), not as a new field
bolted onto `cited_memory_ids` itself. This is the design that adds richer
citation detail **without touching Phase 5's already real-LLM-validated
citation-validation mechanism at all** — the safest possible way to extend
citations, given how much verification that mechanism has already earned.

---

## 15. Evaluation

New dataset, `eval/datasets/document_v1.jsonl`, mirroring `agent_v1.jsonl`'s
exact JSONL shape (`task_id`, `category`, `query`, `seed`, `expected_*`) —
with `seed` now meaning seed *documents*: small inline text fixtures run
through the **real** ingestion pipeline (a new `_seed_documents` helper,
analogous to `eval/retrieval_runner.py`'s `_seed_memories`, but exercising
real parsing/chunking/extraction rather than calling `MemoryService.create`
directly, since the pipeline stages being evaluated here — chunking,
document extraction, cross-source dedup — are exactly what a
memory-only seed would skip).

**Task categories** (per the brief, each mapped to a category already
proven feasible by Phase 5's own methodology):

| Category | What it tests |
|---|---|
| Extracting facts from documents | Basic document → memory pipeline correctness |
| Document-derived memory retrieval | `search_memory` finds a document-sourced fact |
| Multi-document reasoning | An answer requiring facts from ≥2 documents |
| Document + conversation reasoning | An answer requiring one document fact and one conversation fact together |
| Temporal document facts | A document asserting a dated fact, queried "as of"/historically |
| Provenance questions | `get_source` returns the correct document/page/chunk |
| Conflicting documents | Two documents assert different facts — exercises the *same* conflict decision engine, cross-source |
| Duplicate information across documents | The same fact in two documents — exercises `DUPLICATE`/`LIKELY_DUPLICATE`, cross-source |
| Document prompt injection | Direct port of Phase 5's real, validated `prompt_injection` task |
| Memory absence | No relevant document exists — the agent must say so |
| Large-document behavior | A synthetic long document, exercising chunking/bounded processing (section 8) end to end |

**Metrics:** the same ones Phase 5's `eval/agent_runner.py` already measures
and reports honestly (recall/precision of `cited_memory_ids` against
expected, tool sequence, tool call count, termination reason, latency, token
usage) plus two document-specific additions: **page-citation correctness**
(did `get_source`'s result name the right page/chunk) and **cross-document
accuracy** (were memories from *both* documents cited when both were
needed).

**No fabricated expected results.** This section defines the dataset and
metrics; actual numbers come only from running the harness after
implementation, exactly as every prior phase's plan has stated and every
prior phase's report has honored (EVALUATION.md section 20).

---

## 16. Baselines

| Baseline | What it measures |
|---|---|
| A — no memory | The floor: an LLM with no access to anything, same role as Phase 5's `A_no_memory`. |
| B — raw document/vector retrieval | A direct chunk-embedding vector search (built **only** as an evaluation-only comparison arm, not shipped in production retrieval, section 11/12) — quantifies what extraction+memory actually *adds* over naive document RAG, directly answering `PRODUCT.md`'s own stated research question. |
| C — memory-only (no documents) | The pre-Phase-6 system, unchanged, run against tasks that happen to only need conversation memory — the backward-compatibility floor. |
| D — document + memory hybrid | Extracted memories from both documents and conversations, via the unchanged `HybridRetrievalService` — no document-specific retrieval channel. Measures retrieval mechanics only, the `E_scripted_mechanics` role from Phase 5's own methodology, adapted. |
| E — persistent-memory agent with document ingestion | The full Phase 6 system, agent + tools + document-derived memory, real-LLM validated exactly like Phase 5's `E_real_llm`/`E_scripted_mechanics` split — reused, not reinvented. |

---

## 17. Testing strategy

**Unit (no database, no network):** parser abstraction (fixture-based —
PDF parsing is local/offline, so this is fast and deterministic, not
merely mocked), chunking (boundary correctness, overlap, heading-context
extraction, the header/footer-stripping heuristic's exact threshold
behavior), the document status state machine (mirrors `test_lifecycle.py`'s
existing shape), MIME/size/type validation guards, filename sanitization,
storage-key generation (never derived from user input), `FakeStorageClient`
(mirrors `test_llm_fake.py`'s test pattern).

**Integration (real Neo4j + PostgreSQL, `FakeLLMClient` + `FakeEmbeddingClient`
+ `FakeStorageClient`, skips cleanly without services — the same guard
every existing integration test already uses):** upload → ingest → chunk →
extract → memory round trip; provenance resolution to page/chunk;
cross-source duplicate detection (a document fact restating a conversation
fact) and cross-source conflict; idempotency (upload the same file twice,
extract the same chunk twice — mirrors extraction's existing "run twice, no
duplicate memory" test exactly); cross-user isolation (a document belonging
to User A is never visible to User B, at every layer — mirrors every
existing isolation test's exact pattern); large-document bounded processing
at the configured limits; security (a malformed PDF fixture never crashes a
worker; an adversarial prompt-injection document fixture, mirroring Phase
5's real `prompt_injection` case).

**Evaluation tests:** `document_v1.jsonl` run through the deterministic
harness by default (`FakeLLMClient`, free, in CI), with an opt-in real-LLM
run mirroring `eval/agent_runner.py`'s exact `E_real_llm` methodology
(including its honest "scripted vs. real" labeling discipline).

**Reuse, explicitly named:** `tests/integration/conftest.py`'s `db`/
`graph`/`user_id`/`cleanup`/`pg_cleanup` fixtures; `test_sql_safety.py`'s
AST audit (covers new repositories automatically, zero changes to the
audit itself); `test_cypher_safety.py` (covers any new Cypher, though
section 3 concluded almost none is needed); the `-m integration`/`-m llm`
pytest marker convention.

---

## 18. Cost and performance

What **should** be measured once implemented (no numbers invented here,
per EVALUATION.md section 20's standing rule):

- Upload time — should be near-instant; no LLM call is in the critical
  path, mirroring "extraction never blocks the response."
- Parsing time — per document, per page; the most variable stage, PDF
  parsing especially.
- Chunking time — near-instant, pure CPU.
- Extraction latency per chunk — reuses `extraction_jobs.latency_ms`,
  already tracked, rolled up per document.
- Embedding latency — reuses existing tracking, unchanged.
- Token usage / LLM cost per document — the sum of its chunk jobs'
  `input_tokens`/`output_tokens`, already tracked per job; a document's
  total cost is a simple aggregate query, not a new tracking mechanism.
- Storage size — bytes per document, cumulative per user; worth surfacing
  since object storage has its own real cost.
- Retrieval latency — unchanged measurement point, already instrumented by
  the existing eval harnesses.

---

## 19. Deployment (revised at approval — zero-cost constraint)

```text
Vercel  →  Railway FastAPI  →  PostgreSQL  →  Neo4j Aura  →  Local disk (v1 storage)
                                                            →  LLM / embedding provider
```

No Docker, no Kubernetes, and **no new external service at all** in v1 —
`LocalDocumentStorage` writes to the managed host's own filesystem, so
there is nothing new to provision, configure a billing account for, or add
to `/health/ready`. This is a deliberate, zero-cost-driven simplification
relative to the original plan's object-storage design (section 10).

**One real limitation this creates, stated honestly rather than hidden:**
local disk on a single managed-host instance is not shared across multiple
instances or survivable across a redeploy that provisions a fresh
filesystem (Railway's own disk is ephemeral across deploys unless a
persistent volume is attached). This is acceptable for a single-instance
deployment and for local development, which is all Phase 6 v1 targets — it
is not acceptable for a future multi-instance production deployment, at
which point a real object-storage backend (still just a config change,
given the `DocumentStorage` Protocol) becomes a genuine, evidence-based
need rather than a speculative one. Named here so it is never mistaken for
an oversight.

Migrations: the same pre-deploy release step (`alembic upgrade head`),
never at application startup, unchanged from every prior phase.

---

## 20. Scope boundaries — what Phase 6 will NOT implement

- Arbitrary file formats — only TXT/Markdown/PDF(text-layer) ship; DOCX is
  explicitly deferred (section 1), not merely unmentioned.
- Video/audio ingestion.
- Autonomous web crawling or URL ingestion — `SourceType.URL` already
  exists as an enum value (unused, like `DOCUMENT` was before this phase)
  but is **not** wired to any ingestion mechanism here; a natural future
  candidate, not built now.
- OCR / scanned-document understanding (section 5).
- Structured table-extraction systems (table → JSON).
- Multi-agent document processing.
- Enterprise document permissions/sharing — a document is owned by exactly
  one user, the same single-tenant-per-user isolation as everything else;
  no "share this document with another user" feature.
- Advanced/semantic chunking (section 6).
- Distributed ingestion infrastructure (Redis/Celery/Kafka/etc., section 8).
- Chunk-level embeddings or raw-chunk retrieval (sections 11/12) — an
  explicit, evidence-gated v2 candidate.
- `DocumentVersion` — considered in section 3's data-model review and
  **not** included: nothing in the brief asks for tracking edits to the
  *same* logical document over time, and the checksum-based dedup (section
  8) only catches byte-identical re-uploads, not edited versions. A
  re-upload of a changed file is simply a new `Document` row in v1.
- User-initiated cancellation of an in-flight ingestion job (section 2).
- **A new review/approval UI or mechanism for document-derived memories —
  not needed at all.** A document-derived `LIKELY_DUPLICATE`/`FLAGGED`/
  `CONFLICTING` candidate flows into the *existing* Phase 4
  `memory_review_candidates` table automatically, since it is produced by
  the same decision engine. This is a positive consequence of reuse, not an
  exclusion — worth stating plainly rather than leaving implicit.

---

## 21. Migration/backward compatibility

- **Existing memories, entities, relationships, embeddings, sources,
  conversations:** untouched. The two widened tables
  (`extraction_jobs`/`extraction_candidates`) receive strictly additive
  nullable columns; a migration-time default (`source_kind='message'`)
  backfills every existing row, so no existing job or candidate's meaning
  changes.
- **Agent behavior:** unchanged for every existing tool; `get_source`'s
  extension is additive (new optional response fields, `None` for every
  pre-existing source), so any existing test asserting message-shaped
  provenance responses keeps passing unmodified.
- **`Source.source_type=DOCUMENT`/`EntityType.DOCUMENT`:** already exist in
  the enum from Phase 1 — using them for the first time is not a schema
  change, it is the first real use of an already-provisioned value.

---

## 22. Architecture decisions (proposed ADRs — not created yet)

1. **"`extraction_jobs`/`extraction_candidates` widened to accept a
   document chunk as a target, alongside a conversation message"** — the
   `source_kind` discriminator decision (section 3), the single most
   consequential integration choice in this plan.
2. **"Document bytes live in object storage; PostgreSQL never stores raw
   file content"** — the storage boundary (section 10), a direct extension
   of ADR 0006's PostgreSQL-boundary precedent to a genuinely new kind of
   content.
3. **"Document chunks are provenance evidence, not a second retrieval
   index"** — the section 11/12 decision that chunk embeddings and raw-chunk
   retrieval are deferred, and memory remains the sole retrieval unit.

No ADR is proposed for the chunking strategy, the header/footer heuristic,
or the document extraction prompt — each is an implementation detail of an
already-decided architecture, justified inline in this plan, matching
Phase 3/4's own restraint about not creating ADRs for implementation
details.

---

## 23. Implementation sequence

```text
1.  ADRs 0012-0014 + evaluation dataset skeleton     no code, mirrors Phase 2's
                                                      "step 2.0" precedent
2.  Storage abstraction                              StorageClient protocol +
                                                      FakeStorageClient + S3 adapter --
                                                      fully independent, testable
                                                      before anything else needs a
                                                      real file (mirrors "Fake*Client
                                                      must exist before anything else
                                                      can be tested")
3.  Data model                                       documents / document_chunks /
                                                      ingestion_jobs tables +
                                                      extraction_jobs / candidates
                                                      widening, one Alembic migration
4.  Document upload endpoint                         validate, checksum-dedup, store
                                                      bytes, create Document row --
                                                      no parsing yet, fully testable
                                                      with FakeStorageClient
5.  Parser abstraction                                TXT/MD trivial; PDF via the one
                                                      new dependency -- pure functions,
                                                      unit-tested in isolation first
                                                      (mirrors similarity.py's "pure
                                                      logic first" ordering)
6.  Chunking                                          deterministic, pure, unit-tested
                                                      in isolation, same reasoning
7.  Ingestion job                                     wires 2-6 together: claim ->
                                                      validate -> parse -> chunk ->
                                                      persist chunks, reusing the
                                                      sweep/lease pattern
8.  Extraction integration                            widened extraction_jobs/candidates
                                                      + document extraction prompt +
                                                      trigger from ingestion completion --
                                                      the actual pipeline reuse point
9.  Provenance                                        get_source extension for
                                                      document-typed sources
10. Embeddings                                        verify only -- no new code;
                                                      embed_and_store_memory already
                                                      fires regardless of source
11. Retrieval integration                             verify only -- no new code, per
                                                      section 12's conclusion
12. Agent integration                                 verify search_memory/get_source
                                                      work end to end with
                                                      document-derived memories; no new
                                                      tool
13. Evaluation                                        document_v1.jsonl + harness,
                                                      mirroring eval/agent_runner.py
14. Security hardening pass                           adversarial fixtures, malformed-
                                                      file tests, cross-user isolation --
                                                      same dedicated-pass precedent as
                                                      every prior phase
15. Deployment verification                           real S3-compatible bucket, real
                                                      Alembic migration against staging
```

Steps 10–11 are listed explicitly, as *verification* steps rather than
*build* steps, because that is the honest state of the work after step 9 —
this plan's central claim throughout (section 0) is that embeddings and
retrieval need no new code, and the implementation sequence should not
imply otherwise.

---

## 24. Risk register

| Risk | Mitigation | How it is tested |
|---|---|---|
| Prompt injection via documents | Inherits ADR 0007's containment + Phase 5's real-LLM-validated agent-side containment | Adversarial document fixture in both the extraction pipeline and the eval harness (direct port of Phase 5's validated `prompt_injection` case) |
| Provenance corruption (wrong page/chunk cited) | Exact-offset chunking, exact-pointer `Source.reference`/`location` lookup (never a search) | Chunking unit tests assert exact `char_start`/`char_end`/`page_start`/`page_end`; integration test asserts `get_source`'s returned page/chunk matches a fixture's known-correct location |
| Duplicate memory creation (same fact in 2 docs, or doc + conversation) | The existing decision engine, now exercised cross-source for the first time | New cross-source duplicate/conflict integration tests (section 17) — a genuinely new test *surface*, not new logic |
| Document extraction hallucination | Same mandatory-grounding, conservative decision engine as conversation extraction — no higher risk by construction | Grounding-rejection tests extended with document fixtures, mirroring `test_extraction_grounding.py`'s existing shape |
| Large files | Bounded by section 8's limits, rejected before parsing | Large-synthetic-document integration test, mirroring `graph_scale_benchmark.py`'s synthetic-scale-testing precedent |
| Cross-user leakage | `UserScopedRepository` + the existing AST audit, unchanged mechanism | Explicit regression test mirroring every prior phase's exact isolation-test pattern |
| Parser failures | Timeout + try/except at the ingestion-job level, never crashes a worker | Deliberately malformed-PDF fixture |
| OCR limitations | Not attempted — the real risk is a scanned PDF silently producing near-empty extraction, not OCR quality itself | Explicit low-text-density detection (section 5), tested directly |
| Cost explosion | `MAX_CHUNKS_PER_DOCUMENT` + the existing `EXTRACTION_DAILY_TOKEN_BUDGET`, now covering document jobs too | Covered by the same bound already relied on for conversation extraction |
| Idempotency | Document-level checksum dedup (new) + the existing 3-layer extraction idempotency (unchanged) | "Upload/ingest the same document twice" integration test, mirroring extraction's existing "run the same extraction twice" test |

**Highest-risk area, named directly:** the `extraction_jobs`/
`extraction_candidates` widening (section 3, ADR 0012). Getting the
`source_kind` discriminator and its `CHECK` constraint wrong would be the
one change in this plan capable of corrupting *existing*, working
conversation-extraction behavior — it deserves the most implementation-time
scrutiny and the most test coverage of anything in this phase, mirroring
how Phase 4 named entity-merge redirect resolution as its own highest-risk
piece for the same reason (a shared, load-bearing mechanism being extended
rather than a purely additive one).

---

## 25. Decision: custom implementation vs. libraries

| Capability | Decision | Why |
|---|---|---|
| TXT/Markdown parsing | Custom | Trivial — stdlib `open()` + decode; heading detection via a small regex. No markdown-rendering library needed, since headings only need *detecting*, not rendering. |
| PDF text extraction | **Lightweight library — `pypdf`** | Pure-Python, no system dependency (unlike wrappers around Poppler/`pdftotext`), actively maintained, narrowly scoped to exactly this one job. Preferred over `pdfplumber` (heavier, brings table-extraction machinery this plan explicitly defers) and over any cloud parsing/OCR API (a new external network dependency plus cost plus exactly the infrastructure category this plan avoids without evidence of need). Isolated to a single `app/documents/parsers/pdf.py` module, mirroring `app/llm/providers/gemini.py`'s "only this file imports the SDK" discipline. |
| DOCX parsing | Deferred (section 1) | If/when added: `python-docx`, the standard lightweight choice, in its own isolated module, same pattern. |
| Chunking | Custom | Simple enough that a library would add indirection without saving real code — matches `app/semantic/similarity.py`'s "pure functions, no I/O" precedent exactly. |
| Storage abstraction | Custom Protocol, stdlib only | `DocumentStorage`/`LocalDocumentStorage` (section 10, revised at approval) uses only `pathlib`/`os` — no library at all. No S3 adapter is built in v1, so no S3 SDK is a dependency yet. |
| Header/footer stripping | Custom | A repeated-line-frequency heuristic, a dozen lines of pure Python, exhaustively unit-testable — a library would be overkill for this narrow, well-defined heuristic. |
| Extraction | **Reuse entirely** | Zero new library — `LLMClient`/`ExtractionRunner`. |
| Embeddings | **Reuse entirely** | Zero new library — `EmbeddingClient`/pgvector. |

**Net new runtime dependency for v1: exactly one — `pypdf`.** Isolated to a
single file, matching the exact discipline `google-genai` (Phase 2) and
`pgvector` (Phase 3) already established: a narrowly-scoped,
single-file-isolated, provider-specific dependency, never a general-purpose
framework. (Revised at approval: the original draft of this plan also
counted `aioboto3` here; removed per the zero-cost constraint and section
10's revision — it is not needed until a real S3-compatible backend is
built, which v1 does not do.)

---

## 26. Final recommendation

**Recommended architecture:** document → ingestion job (new: validate,
parse, chunk) → the *existing* extraction pipeline, decision engine, Neo4j
memory model, embedding pipeline, hybrid retrieval, and agent — with
exactly two genuinely new architectural surfaces: a local-filesystem
storage layer (nothing in Phases 1–5 ever stored a file; zero-cost by
construction, section 10) and a narrow widening of two
existing PostgreSQL tables to accept a document chunk as a target alongside
a conversation message. Every other integration point is reuse, not new
code — the governing constraint from section 0, held throughout.

**Exact v1 scope:** TXT, Markdown, and text-layer PDF (no scanned/OCR PDFs,
no DOCX yet); deterministic fixed-size chunking with overlap and
page/heading metadata; one `ExtractionJob` per chunk through the unchanged
decision engine; document provenance via the existing `Source` node's
already-provisioned `DOCUMENT` type, exposed through an extended
`get_source`; memory-only retrieval (no chunk-level index); no new agent
tool.

**Major data model:** new `documents`, `document_chunks`, `ingestion_jobs`
(PostgreSQL); `extraction_jobs`/`extraction_candidates` widened with a
`source_kind` discriminator; zero Neo4j schema change.

**Major API surface:** a new `app/api/routes/documents.py` router —
`POST /documents` (upload), `GET /documents`, `GET /documents/{id}`,
`GET /documents/{id}/chunks`, `GET /documents/{id}/status`. No changes to
`agent.py`, `memories.py`, or `search.py`.

**Implementation sequence:** the 15 steps in section 23, storage and data
model first (both independently testable before anything else depends on
them), the actual pipeline-reuse integration in the middle, evaluation and
hardening last — the same shape every prior phase's plan has used.

**Evaluation strategy:** `eval/datasets/document_v1.jsonl`, 11 task
categories, 5 baselines (A–E), real-LLM validation mirroring Phase 5's
proven `E_scripted_mechanics`/`E_real_llm` methodology exactly — no
fabricated numbers, ever.

**Major risks:** the `extraction_jobs`/`extraction_candidates` widening
(highest — a shared mechanism, not a purely additive one), prompt injection
via documents (inherited containment, real-LLM tested), provenance
correctness (exact-offset, exactly testable), and large-file bounded
processing (measured, not assumed) — each with a named, proportionate
mitigation in section 24.

**Proposed ADRs:** 0012 (extraction target widening), 0013 (object storage
boundary), 0014 (chunks as evidence, not a second retrieval index).

**Explicit deferred features:** DOCX; scanned-PDF/OCR; structured table
extraction; chunk-level embeddings and raw-chunk retrieval; URL/web
ingestion (despite `SourceType.URL` already existing); document versioning;
ingestion cancellation; document sharing/permissions; any new queue
infrastructure (Redis/Celery/Kafka) — restated fully in section 20.

---

## Summary: what this plan does and does not authorize

**Authorized once approved:** implementing sections 1–25 and the
implementation order in section 23, in that order, with the same
verification discipline as every prior phase — real Neo4j + PostgreSQL
testing throughout, a real-LLM evaluation pass mirroring Phase 5's own, a
dedicated security/hardening pass before final sign-off, no fabricated
evaluation numbers.

**Not authorized, and not part of this plan:** DOCX or any other format
beyond TXT/Markdown/text-PDF, OCR or scanned-document support, structured
table extraction, chunk-level embeddings or a second retrieval system,
document sharing between users, a new agent tool, Redis/Celery/Kafka or any
new infrastructure category, or any change to the exact-triple check, the
conservative decision policy, the Neo4j-is-canonical boundary, or the
agent's citation-validation mechanism.

Waiting for approval before writing any Phase 6 application code.
