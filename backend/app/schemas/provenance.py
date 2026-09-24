"""Response schema for `GET /memories/{id}/provenance`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.conversation import Message
from app.models.document import DocumentChunkSummary, DocumentSummary
from app.models.domain import Memory, Source


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Source
    #: Resolved from Neo4j's Source.reference into the actual PostgreSQL message,
    #: when the source is a conversation message and that message still exists.
    #: None for non-message sources or if the message could not be resolved.
    message: Message | None = None
    #: Resolved when the source is a document (Phase 6, ADR 0012): `document`
    #: from `Source.reference`, `document_chunk` from the chunk id embedded in
    #: `Source.location`. Both None for non-document sources or if either
    #: could no longer be resolved (e.g. the document was later deleted).
    document: DocumentSummary | None = None
    document_chunk: DocumentChunkSummary | None = None


class ProvenanceView(BaseModel):
    """Answers "why does the system believe this?" -- Memory -> Source -> Message."""

    model_config = ConfigDict(extra="forbid")

    memory: Memory
    sources: list[SourceProvenance]
