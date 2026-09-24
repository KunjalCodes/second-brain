"""Conversation persistence.

PostgreSQL application state (docs/adr/0006) -- not canonical memory. Every query
goes through `UserScopedRepository`, so `user_id` filtering cannot be forgotten at a
call site (see `app/repositories/sql_base.py`).
"""

from __future__ import annotations

from datetime import datetime

from app.db.models import Conversation as ConversationRow
from app.models.conversation import Conversation
from app.repositories.sql_base import UserScopedRepository


def _to_domain(row: ConversationRow) -> Conversation:
    return Conversation(
        id=row.id,
        user_id=row.user_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_message_at=row.last_message_at,
    )


class ConversationRepository(UserScopedRepository):
    async def create(
        self, *, conversation_id: str, title: str | None, now: datetime
    ) -> Conversation:
        row = ConversationRow(
            id=conversation_id,
            user_id=self._user_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get(self, *, conversation_id: str) -> Conversation | None:
        row = await self._session.get(ConversationRow, conversation_id)
        if row is None or row.user_id != self._user_id:
            # Reported identically to "does not exist": the API must never confirm
            # that another user's conversation exists.
            return None
        return _to_domain(row)

    async def get_for_update(self, *, conversation_id: str) -> ConversationRow | None:
        """Row-locked fetch, used to serialize message-sequence assignment.

        Returns the ORM row (not the domain model) because the caller -- only
        `MessageRepository.append` -- needs to mutate `last_message_at`/`updated_at`
        in the same transaction.
        """
        stmt = self._select(ConversationRow).where(
            ConversationRow.id == conversation_id
        ).with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(self, *, limit: int, offset: int) -> list[Conversation]:
        stmt = (
            self._select(ConversationRow)
            .order_by(ConversationRow.last_message_at.desc().nulls_last(),
                      ConversationRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_to_domain(row) for row in result.scalars()]

    async def count(self) -> int:
        from sqlalchemy import func

        # Built on `_select` (never a bare `select(...)`) so the isolation filter
        # is inherited rather than re-specified.
        stmt = self._select(ConversationRow).with_only_columns(func.count())
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
