# ADR 0012 — Extraction targets widen to messages OR document chunks, one pipeline

**Status:** Accepted (Phase 6)

## Context

Phase 6 adds documents as a second source of persistent memory. The explicit,
repeated project constraint (CLAUDE.md, docs/PHASE6_PLAN.md) is that documents must
become **another source feeding the same extraction pipeline**, not a second
retrieval system or a second decision engine. Concretely: a `DocumentChunk` needed
to play the exact role a `Message` already plays in `app/extraction/runner.py` --
grounding, entity resolution, duplicate/conflict classification, temporal
supersession, provenance, and the canonical Neo4j write must all be the *same code*
for both.

Two designs were possible: a parallel `document_extraction_jobs` /
`document_extraction_candidates` pair of tables with their own runner, mirroring
`extraction_jobs`/`extraction_candidates` structurally but duplicating the claim/
lease/retry machinery and the decision engine's call sites; or widening the existing
tables and runner to carry an explicit discriminator between the two source kinds.
The former was rejected outright -- it is precisely the "second decision engine"
Phase 5's ADR 0011 already argued against for the agent's write path, and the same
argument applies here with more force, since a document can produce far more
extraction jobs than a single agent turn.

## Decision

`extraction_jobs` and `extraction_candidates` are widened, not duplicated:

* A new `source_kind` column on `extraction_jobs` (`'message'` | `'document_chunk'`,
  default `'message'` so every pre-Phase-6 row's meaning is unchanged) says which of
  two mutually-exclusive target pairs is populated: `(conversation_id,
  target_message_id)` or `(document_id, target_chunk_id)`. A database `CHECK`
  constraint enforces exactly one pair is set, matching `source_kind` -- not merely
  convention.
* Two separate unique constraints (`extraction_jobs_idempotency` on
  `target_message_id`, `extraction_jobs_chunk_idempotency` on `target_chunk_id`)
  give each source kind its own real idempotency protection. This works without a
  single constraint needing to express an XOR, because PostgreSQL treats `NULL` as
  distinct from `NULL` in a unique constraint: every document job's
  `target_message_id` is `NULL` and never collides with another `NULL`, and
  symmetrically for message jobs' `target_chunk_id`.
* `extraction_candidates` gets the identical treatment: `source_message_id` becomes
  nullable, a new nullable `source_chunk_id` is added, and a `CHECK` constraint
  requires exactly one of the two.
* `window_start_sequence`/`window_end_sequence` are reused unchanged for document
  jobs: a chunk's `ordinal` plays the exact role a message's `sequence` plays today,
  so "window" becomes "a range of adjacent chunk ordinals" with zero new columns.
* `ExtractionRunner.run()` branches once, near the top, on `job.source_kind`, to load
  either a message window (`MessageRepository.list_by_sequence_range`, unchanged) or
  a chunk window (`DocumentChunkRepository.list_by_ordinal_range`, new but structurally
  identical) and to build the matching prompt (`build_extraction_prompt` vs the new
  `build_document_extraction_prompt`). Both branches then feed the *same*
  `_apply_candidate`/`_persist` call -- generalized to take `target_content`/
  `source_type`/`source_reference` instead of a `Message`-typed parameter, since a
  `Message` and a `DocumentChunk` both expose a plain `.content` string.
* `check_grounding()` needed **zero changes**: it already took plain strings
  (`target_message_content`/`window_text`), never a typed `Message`.
* `CandidateMemory.source_message_id` (the LLM output contract) needed **zero schema
  changes** either: it is reused as-is to carry a chunk id for document extraction,
  verified to be used only for audit/evidence logging (`_flag_ambiguous_entities`),
  never for grounding validation. The LLM-facing contract (`ExtractionResult`/
  `CandidateMemory`) is therefore byte-identical between the two paths.
* A document-sourced memory's `Source.reference` is the document id (a document has
  no per-chunk identity the way a message id already is one), so the specific chunk
  id is threaded through `Source.location` instead, as a `"chunk:<id>:"` prefix
  ahead of the usual `"{start}:{end}"` span (`app.models.document.
  format_chunk_source_location`/`parse_chunk_id_from_location`). A MESSAGE source's
  `location` is completely unaffected -- still exactly `"{start}:{end}"`.

## Consequences

* **One decision engine, provably.** `tests/integration/test_document_ingestion_pipeline.py`
  exercises the full upload -> chunk -> extract -> memory path and asserts the
  resulting `Source` shape; `TestConversationPathIsUnchanged` in the same file pins
  the pre-Phase-6 MESSAGE-source shape as an explicit regression guard, since
  `_apply_candidate`/`_persist` now have a second caller path.
* **`ExtractionJobRepository` gained a second job-creation method,
  `create_document_chunk_job`, deliberately separate from `create_job`** rather than
  branching inside it, so the well-tested conversation path's own method is never
  touched.
* A document's coarse `DocumentStatus` (COMPLETE/PARTIALLY_COMPLETE) has no
  conversation-path analogue -- a message has no parent entity whose status depends
  on sibling messages, so `ExtractionRunner._maybe_finalize_document` is a
  document-only addition, called only when `is_document_job`, rolling a document's
  status up once every one of its chunks reaches a terminal `extraction_status`.
* The widened tables mean a future third source kind (e.g. a URL fetch) would extend
  the same `source_kind` enum and the same two columns-pairs pattern, rather than
  adding a third parallel pipeline -- though no such extension is implemented or
  planned now.
