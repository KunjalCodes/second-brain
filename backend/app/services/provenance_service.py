"""Resolves a memory's provenance chain: Memory -> Source -> Message -> Conversation.

The one place this codebase deliberately crosses the PostgreSQL/Neo4j boundary in a
read: a Source's `reference` is a message id (docs/PHASE2_PLAN.md section 2), and
answering "why does the system believe this?" means following that id back into
PostgreSQL. This is read-only and one-directional, consistent with docs/adr/0006 --
nothing here writes to either store or copies data between them.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SourceType
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

        resolved: list[SourceProvenance] = []
        for source in detail.sources:
            message = None
            if source.source_type is SourceType.MESSAGE and source.reference:
                message = await messages.get(message_id=source.reference)
            resolved.append(SourceProvenance(source=source, message=message))

        return ProvenanceView(memory=detail.memory, sources=resolved)
