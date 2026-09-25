"""Message persistence.

PostgreSQL application state (docs/adr/0006) -- not canonical memory.

Message ordering is deterministic by design: `sequence` is a monotonic integer
per conversation, assigned inside a transaction that holds a row lock on the parent
conversation (`ConversationRepository.get_for_update`), so two concurrent appends to
the *same* conversation cannot race for the same sequence number. Appends to
*different* conversations are unaffected -- the lock is per-row, not per-table.
Timestamps are never used as the sole ordering key: two messages can share a
microsecond, but never a sequence number.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func

from app.db.models import Message as MessageRow
from app.models.conversation import ExtractionStatus, Message, MessageRole
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.sql_base import UserScopedRepository


def _to_domain(row: MessageRow) -> Message:
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        role=row.role,
        content=row.content,
        sequence=row.sequence,
        token_count=row.token_count,
        created_at=row.created_at,
        extraction_status=row.extraction_status,
        extraction_gate_reason=row.extraction_gate_reason,
    )


class ConversationNotFound(Exception):
    """Raised by `append` when the conversation does not exist for this user.

    A distinct exception rather than `app.core.errors.NotFoundError` so the
    repository layer stays free of the HTTP-facing error taxonomy -- the service
    layer translates it, exactly as `MemoryRepository` lets `NotFoundError` (a
    genuine domain error) surface directly. This one deliberately isn't a domain
    error: it's a repository-layer signal caught before the service ever sees it.
    """


class MessageRepository(UserScopedRepository):
    async def append(
        self,
        *,
        message_id: str,
        conversation_id: str,
        role: MessageRole,
        content: str,
        token_count: int | None,
        now: datetime,
    ) -> Message:
        """Insert a message with the next sequence number for its conversation.

        Raises `ConversationNotFound` if the conversation does not exist, or exists
        but belongs to a different user -- the two cases are indistinguishable by
        design, so the API can never confirm another user's conversation exists.
        """
        conversations = ConversationRepository(self._session, user_id=self._user_id)
        conversation = await conversations.get_for_update(conversation_id=conversation_id)
        if conversation is None:
            raise ConversationNotFound(conversation_id)

        next_sequence = await self._next_sequence(conversation_id)

        row = MessageRow(
            id=message_id,
            conversation_id=conversation_id,
            user_id=self._user_id,
            role=role.value,
            content=content,
            sequence=next_sequence,
            token_count=token_count,
            created_at=now,
            extraction_status=ExtractionStatus.PENDING.value,
        )
        self._session.add(row)

        conversation.updated_at = now
        conversation.last_message_at = now

        await self._session.flush()
        return _to_domain(row)

    async def _next_sequence(self, conversation_id: str) -> int:
        stmt = (
            self._select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .with_only_columns(func.max(MessageRow.sequence))
        )
        result = await self._session.execute(stmt)
        current_max = result.scalar_one_or_none()
        return 0 if current_max is None else current_max + 1

    async def get(self, *, message_id: str) -> Message | None:
        row = await self._session.get(MessageRow, message_id)
        if row is None or row.user_id != self._user_id:
            return None
        return _to_domain(row)

    async def get_many(self, *, message_ids: list[str]) -> dict[str, Message]:
        """Batched `get` (Phase 8 evidence endpoint): one query for many ids,
        user-scoped like every other read; unknown or foreign ids are absent."""
        if not message_ids:
            return {}
        stmt = self._select(MessageRow).where(MessageRow.id.in_(message_ids))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.id: _to_domain(row) for row in rows}

    async def list(
        self, *, conversation_id: str, limit: int, offset: int
    ) -> list[Message]:
        stmt = (
            self._select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.sequence)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(row) for row in result.scalars()]

    async def list_by_sequence_range(
        self, *, conversation_id: str, start_sequence: int, end_sequence: int
    ) -> list[Message]:
        """The window an extraction job reads -- inclusive on both ends."""
        stmt = (
            self._select(MessageRow)
            .where(
                MessageRow.conversation_id == conversation_id,
                MessageRow.sequence >= start_sequence,
                MessageRow.sequence <= end_sequence,
            )
            .order_by(MessageRow.sequence)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(row) for row in result.scalars()]

    async def set_extraction_status(
        self,
        *,
        message_id: str,
        status: ExtractionStatus,
        gate_reason: str | None = None,
    ) -> None:
        stmt = (
            self._update(MessageRow)
            .where(MessageRow.id == message_id)
            .values(extraction_status=status.value, extraction_gate_reason=gate_reason)
        )
        await self._session.execute(stmt)

    async def count(self, *, conversation_id: str) -> int:
        stmt = (
            self._select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .with_only_columns(func.count())
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
