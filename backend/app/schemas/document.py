"""Request/response schemas for document endpoints (Phase 6, PHASE6_PLAN.md
section 3). Never `Document.storage_key` -- an internal filesystem pointer,
not application data (mirrors `DocumentSummary`'s own rule in
`app/models/document.py`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.conversation import ExtractionStatus
from app.models.document import Document, DocumentChunk, DocumentStatus


class DocumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    filename: str
    content_type: str
    size_bytes: int
    page_count: int | None = None
    status: DocumentStatus
    error_code: str | None = None
    error_detail: str | None = None
    uploaded_at: datetime
    processed_at: datetime | None = None

    @classmethod
    def from_domain(cls, document: Document) -> DocumentResponse:
        return cls(
            id=document.id,
            filename=document.filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            page_count=document.page_count,
            status=document.status,
            error_code=document.error_code,
            error_detail=document.error_detail,
            uploaded_at=document.uploaded_at,
            processed_at=document.processed_at,
        )


class DocumentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentResponse]


class DocumentChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ordinal: int
    content: str
    page_start: int | None = None
    page_end: int | None = None
    heading_context: str | None = None
    extraction_status: ExtractionStatus

    @classmethod
    def from_domain(cls, chunk: DocumentChunk) -> DocumentChunkResponse:
        return cls(
            id=chunk.id,
            ordinal=chunk.ordinal,
            content=chunk.content,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            heading_context=chunk.heading_context,
            extraction_status=chunk.extraction_status,
        )


class DocumentChunkListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[DocumentChunkResponse]
