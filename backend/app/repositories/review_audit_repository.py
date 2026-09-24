"""The append-only review audit log (ADR 0010, PHASE4_PLAN.md section 11a).

`record` is the only write this repository ever offers -- there is deliberately no
update/delete, matching the table's own "never edited after insert" contract
(`app.db.models.ReviewAuditLog`). A correction to a review decision is a new row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, insert

from app.db.models import ReviewAuditLog as AuditRow
from app.repositories.sql_base import UserScopedRepository


class ReviewAuditRepository(UserScopedRepository):
    async def record(
        self,
        *,
        entry_id: str,
        action: str,
        target_type: str,
        target_id: str,
        previous_state: dict[str, Any],
        resulting_state: dict[str, Any],
        reason: str | None,
        reviewer_id: str,
        now: datetime,
    ) -> None:
        """A Core `insert` (not ORM `session.add`) so the row is visible to a
        later read in the SAME session without relying on autoflush -- this
        project's sessionmaker is configured `autoflush=False`
        (`app.db.engine.Database.connect`), matching every other repository's
        write path.
        """
        stmt = insert(AuditRow).values(
            id=entry_id,
            user_id=self._user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            previous_state=previous_state,
            resulting_state=resulting_state,
            reason=reason,
            reviewer_id=reviewer_id,
            created_at=now,
        )
        await self._session.execute(stmt)

    async def list_recent(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        stmt = (
            self._select(AuditRow)
            .order_by(AuditRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_row_to_dict(row) for row in result.scalars()]

    async def count(self) -> int:
        stmt = self._select_columns(AuditRow, func.count())
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_for_target(self, *, target_type: str, target_id: str) -> list[dict[str, Any]]:
        stmt = (
            self._select(AuditRow)
            .where(AuditRow.target_type == target_type, AuditRow.target_id == target_id)
            .order_by(AuditRow.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return [_row_to_dict(row) for row in result.scalars()]


def _row_to_dict(row: AuditRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "previous_state": row.previous_state,
        "resulting_state": row.resulting_state,
        "reason": row.reason,
        "reviewer_id": row.reviewer_id,
        "created_at": row.created_at,
    }
