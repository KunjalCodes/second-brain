# ADR 0014 — Document chunks are provenance/evidence, not a second retrieval index

**Status:** Accepted (Phase 6)

## Context

A common "RAG over documents" design stores chunk embeddings in a vector index and
retrieves top-k chunks directly in response to a query, entirely separate from
whatever other memory the system holds. CLAUDE.md's own project identity section is
explicit that this project must **not** become that: "This is NOT intended to be...
a simple RAG chatbot... a wrapper around an existing memory library," and the Phase 6
approval message restated the constraint in document-specific terms: "Documents are
another SOURCE OF PERSISTENT MEMORY... Do NOT build: a PDF chatbot, a second RAG
architecture, a second vector index for document chunks, a second memory system.
Canonical semantic knowledge remains Memory. Document chunks are provenance/
supporting evidence."

`memory_embeddings` (pgvector, Phase 3, ADR 0009) already indexes `Memory` content for
semantic search. The question this ADR settles: does Phase 6 add a second,
parallel embedding index over `document_chunks.content`, queried independently
alongside `memory_embeddings`?

## Decision

**No second vector index is created for document chunks.** `document_chunks` has no
embedding column and no corresponding pgvector index. A chunk's content is consumed
in exactly two ways:

1. As extraction **input** -- the same way a `Message`'s content is extraction input
   (ADR 0012) -- read once by `ExtractionRunner`, turned into zero or more
   `CandidateMemory` proposals, and then the canonical output of that process (a
   `Memory` node, embedded via the existing Phase 3 pipeline exactly like any other
   memory) is what becomes searchable.
2. As **provenance/evidence** -- when a memory did originate from a document, its
   `Source.location` carries the originating chunk's id (ADR 0012), and
   `ProvenanceService`/`app.agent.tools.get_source` resolve that id back to the
   chunk's content, page range, and heading context on demand, for citation and
   inspection. This is read-through, on a specific already-known memory's already-known
   source -- never a ranked query over chunk content in isolation.

`HybridRetrievalService` (vector + graph + keyword over `Memory`, Phase 3/4) is
**not** extended with a third, chunk-level leg. A query never retrieves "the most
relevant chunks"; it retrieves the most relevant *memories*, which may happen to have
been extracted from a document, exactly the same as it may have been extracted from a
conversation message -- the retrieval layer does not need to know or care which.

## Consequences

* **There is exactly one thing in this system that is "the answer": `Memory`.** A
  document chunk is never itself returned as a search result or reasoned over
  directly by the agent; it is only ever reached by following a specific memory's
  provenance chain backward, the same epistemic status a `Message` already has.
* **No duplicate extraction-vs-retrieval inconsistency is possible.** Since nothing
  ranks or serves chunk content independently, there is no way for "what the
  document-chunk index says" and "what canonical Memory says" to disagree --
  `document_chunks` is not a second source of truth a user or agent could be shown,
  it is supporting material for a Memory that already went through the full
  grounding/resolution/decision pipeline.
* **A real, accepted limitation**: a fact present in a document but not extracted as
  a `CandidateMemory` by the LLM (e.g. `persistence="temporary"`, or the extraction
  gate's own conservative judgment) is not separately retrievable by embedding
  similarity over the raw chunk text the way a naive document-RAG system would allow.
  This is the intended, stated trade-off (CLAUDE.md: "prioritize... the quality of the
  memory system," not raw recall over uploaded text) -- v1 does not revisit it, and no
  chunk-level index is added as a fallback.
* `document_chunk_size_chars`/`document_chunk_overlap_chars` are tuned for extraction
  window quality (the same concern `EXTRACTION_WINDOW_SIZE` already tunes for
  messages), not for retrieval-chunk granularity -- there is no retrieval consumer of
  chunk boundaries to tune for.
