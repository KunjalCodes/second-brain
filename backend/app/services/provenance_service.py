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
from app.models.domain import Source
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
        resolved = await self.resolve_sources(user_id=user_id, sources=detail.sources)
        return ProvenanceView(memory=detail.memory, sources=resolved)

    async def resolve_sources(
        self, *, user_id: str, sources: list[Source]
    ) -> list[SourceProvenance]:
        """Resolve Sources into messages / documents / chunks, in input order.

        Batched (Phase 8): at most one query per referent kind however many
        sources are passed, so a caller resolving many memories' sources at
        once (the graph evidence endpoint) does not issue one query per
        source. Semantics are exactly the per-source ones: a referent that
        cannot be found for this user resolves to None, never an error.
        """
        message_ids = sorted(
            {s.reference for s in sources if s.source_type is SourceType.MESSAGE and s.reference}
        )
        document_ids = sorted(
            {s.reference for s in sources if s.source_type is SourceType.DOCUMENT and s.reference}
        )
        chunk_ids = sorted(
            {
                chunk_id
                for s in sources
                if s.source_type is SourceType.DOCUMENT
                and s.reference
                and (chunk_id := parse_chunk_id_from_location(s.location)) is not None
            }
        )
        messages = await MessageRepository(self._session, user_id=user_id).get_many(
            message_ids=message_ids
        )
        documents = await DocumentRepository(self._session, user_id=user_id).get_many(
            document_ids=document_ids
        )
        chunks = await DocumentChunkRepository(self._session, user_id=user_id).get_many(
            chunk_ids=chunk_ids
        )

        resolved: list[SourceProvenance] = []
        for source in sources:
            message = None
            document = None
            document_chunk = None
            if source.source_type is SourceType.MESSAGE and source.reference:
                message = messages.get(source.reference)
            elif source.source_type is SourceType.DOCUMENT and source.reference:
                doc = documents.get(source.reference)
                if doc is not None:
                    document = DocumentSummary(
                        id=doc.id, filename=doc.filename, uploaded_at=doc.uploaded_at
                    )
                chunk_id = parse_chunk_id_from_location(source.location)
                if chunk_id is not None:
                    chunk = chunks.get(chunk_id)
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
            found: bool | None = None
            if source.source_type is SourceType.MESSAGE:
                found = message is not None
            elif source.source_type is SourceType.DOCUMENT:
                wants_chunk = parse_chunk_id_from_location(source.location) is not None
                found = document is not None and (document_chunk is not None or not wants_chunk)
            resolved.append(
                SourceProvenance(
                    source=source,
                    message=message,
                    document=document,
                    document_chunk=document_chunk,
                    resolved=found,
                )
            )
        return resolved
