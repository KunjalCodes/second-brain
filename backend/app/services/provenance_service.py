"""Resolves a memory's provenance chain: Memory -> Source -> Message/Document.

The one place this codebase deliberately crosses the PostgreSQL/Neo4j boundary in a
read: a Source's `reference` is a message id or document id (docs/PHASE2_PLAN.md
section 2; Phase 6 ADR 0012 for the document case), and answering "why does the
system believe this?" means following that id back into PostgreSQL. This is
read-only and one-directional, consistent with docs/adr/0006 -- nothing here writes
to either store or copies data between them.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentChunkSummary, DocumentSummary, parse_chunk_id_from_location
from app.models.enums import SourceType
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.message_repository import MessageRepository
from app.schemas.provenance import ProvenanceView, SourceProvenance
from app.services.memory_service import MemoryService


class ProvenanceService:
    def __init__(self, session: AsyncSession, memory_service: MemoryService) -> None:
        self._session = session
        self._memory_service = memory_service

    async def get_provenance(self, *, user_id: str, memory_id: str) -> ProvenanceView:
        """Raises NotFoundError (via `MemoryService.get`) if the memory does not
        exist for this user -- the same isolation guarantee as every other memory
        read.
        """
        detail = await self._memory_service.get(user_id=user_id, memory_id=memory_id)
        messages = MessageRepository(self._session, user_id=user_id)
        documents = DocumentRepository(self._session, user_id=user_id)
        chunks = DocumentChunkRepository(self._session, user_id=user_id)

        resolved: list[SourceProvenance] = []
        for source in detail.sources:
            message = None
            document = None
            document_chunk = None
            if source.source_type is SourceType.MESSAGE and source.reference:
                message = await messages.get(message_id=source.reference)
            elif source.source_type is SourceType.DOCUMENT and source.reference:
                doc = await documents.get(document_id=source.reference)
                if doc is not None:
                    document = DocumentSummary(
                        id=doc.id, filename=doc.filename, uploaded_at=doc.uploaded_at
                    )
                chunk_id = parse_chunk_id_from_location(source.location)
                if chunk_id is not None:
                    chunk = await chunks.get(chunk_id=chunk_id)
                    if chunk is not None:
                        document_chunk = DocumentChunkSummary(
                            id=chunk.id,
                            # `chunk_size_chars` can be configured up to
                            # 20,000 (app.core.config.Settings), well past
                            # this summary's own 4,000-char cap -- truncate
                            # rather than let a large chunk fail validation.
                            content=chunk.content[:4000],
                            page_start=chunk.page_start,
                            page_end=chunk.page_end,
                            heading_context=chunk.heading_context,
                        )
            resolved.append(
                SourceProvenance(
                    source=source, message=message, document=document, document_chunk=document_chunk
                )
            )

        return ProvenanceView(memory=detail.memory, sources=resolved)
