"""Document ingestion domain models (Phase 6, PHASE6_PLAN.md section 3).

PostgreSQL application/ingestion state, not canonical memory -- exactly the
same status as `app.models.conversation`'s `Message`/`Conversation` (ADR
0006). A `DocumentChunk` plays the role a `Message` plays for the extraction
pipeline; `document_chunks.extraction_status` reuses
`app.models.conversation.ExtractionStatus` directly rather than duplicating
an identical enum.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import ExtractionStatus


class DocumentStatus(StrEnum):
    """Coarse, document-level lifecycle (PHASE6_PLAN.md section 2).

    Finer-grained progress lives on `IngestionJob.status` (validate/parse/
    chunk) and each `DocumentChunk.extraction_status` (extract) --
    `DocumentStatus` is the roll-up a user/API consumer actually wants.
    """

    UPLOADED = "uploaded"
    VALIDATING = "validating"
    PARSING = "parsing"
    CHUNKING = "chunking"
    CHUNKED = "chunked"
    EXTRACTING = "extracting"
    COMPLETE = "complete"
    PARTIALLY_COMPLETE = "partially_complete"
    FAILED = "failed"


class IngestionJobStatus(StrEnum):
    """Mirrors `app.models.extraction.ExtractionJobStatus` exactly -- the
    same claim/lease/retry vocabulary, applied to VALIDATING->PARSING->
    CHUNKING instead of the LLM call.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


class Document(BaseModel):
    """One uploaded file. Never holds the file's bytes -- `storage_key` is
    an opaque pointer into `DocumentStorage` (section 10).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    filename: str
    content_type: str
    size_bytes: int
    content_hash: str
    storage_key: str
    page_count: int | None = None
    status: DocumentStatus
    error_code: str | None = None
    error_detail: str | None = None
    uploaded_at: datetime
    processed_at: datetime | None = None


class DocumentChunk(BaseModel):
    """One deterministic slice of a document's parsed text -- the document
    analogue of `Message` for the extraction pipeline (ADR 0012).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    document_id: str
    user_id: str
    ordinal: int
    content: str
    content_hash: str
    page_start: int | None = None
    page_end: int | None = None
    heading_context: str | None = None
    char_start: int
    char_end: int
    extraction_status: ExtractionStatus
    created_at: datetime


class IngestionJob(BaseModel):
    """VALIDATING -> PARSING -> CHUNKING, one row per document."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    document_id: str
    status: IngestionJobStatus
    attempts: int
    locked_at: datetime | None = None
    locked_by: str | None = None
    next_attempt_at: datetime | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class DocumentSummary(BaseModel):
    """Compact document identity, for provenance display (section 4) --
    never the full `Document` (which is an internal record, not something
    to echo back verbatim into a citation).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    uploaded_at: datetime


class DocumentChunkSummary(BaseModel):
    """Compact chunk content, for provenance display (section 4)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    content: str = Field(max_length=4000)
    page_start: int | None = None
    page_end: int | None = None
    heading_context: str | None = None


#: The prefix `ExtractionRunner._persist` writes into a document-sourced
#: memory's `Source.location` (ADR 0012, PHASE6_PLAN.md section 4) --
#: `f"{_CHUNK_LOCATION_PREFIX}{chunk_id}:{start}:{end}"`. A document's
#: `Source.reference` is the document id (a message id has no equivalent
#: need: it already IS the precise location), so the chunk id has to live
#: somewhere else for provenance to resolve back to an exact passage;
#: `location` is the only other free-form field `Source` has. Producer
#: (`app/extraction/runner.py`) and parser (`app/services/provenance_service.py`)
#: both use these two functions rather than duplicating the string format.
_CHUNK_LOCATION_PREFIX = "chunk:"


def format_chunk_source_location(chunk_id: str) -> str:
    """Build the `location` prefix for a document-chunk-sourced memory."""
    return f"{_CHUNK_LOCATION_PREFIX}{chunk_id}"


def parse_chunk_id_from_location(location: str | None) -> str | None:
    """The chunk id embedded in a document source's `location`, or None if
    `location` has no document-chunk prefix (e.g. a MESSAGE source's plain
    `"{start}:{end}"`, or no location at all).
    """
    if location is None or not location.startswith(_CHUNK_LOCATION_PREFIX):
        return None
    rest = location[len(_CHUNK_LOCATION_PREFIX) :]
    chunk_id, _, _span = rest.partition(":")
    return chunk_id or None
