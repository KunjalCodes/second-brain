"""Persistence for flagged-but-never-merged entity ambiguity (Scenario G).

A wrong entity merge destroys memory; a missed one is only an inconvenience. So
this repository records a *suspicion* -- two entity ids that might name the same
real-world thing, plus the evidence -- for later, better-evidenced review. It never
merges, renames, or otherwise changes either entity (docs/adr/0007,
`app/extraction/resolution.py`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app.db.models import EntityMergeCandidate as MergeRow
from app.repositories.sql_base import UserScopedRepository


class EntityMergeCandidateRepository(UserScopedRepository):
    async def record_candidate(
        self,
        *,
        candidate_id: str,
        entity_id_a: str,
        entity_id_b: str,
        evidence: dict[str, Any],
        score: float | None,
        now: datetime,
    ) -> None:
        """Flag a pair for review, ordering the ids so `(a, b)` and `(b, a)` from
        two different candidates land on the same row instead of two.
        """
        first, second = sorted((entity_id_a, entity_id_b))
        stmt = (
            insert(MergeRow)
            .values(
                id=candidate_id,
                user_id=self._user_id,
                entity_id_a=first,
                entity_id_b=second,
                evidence=evidence,
                score=score,
                status="unreviewed",
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "entity_id_a", "entity_id_b"])
        )
        await self._session.execute(stmt)

    async def list_unreviewed(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        stmt = (
            self._select(MergeRow)
            .where(MergeRow.status == "unreviewed")
            .order_by(MergeRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_row_to_dict(row) for row in result.scalars()]

    async def list_by_status(self, *, status: str, limit: int) -> list[dict[str, Any]]:
        """Every row in a given terminal status, bounded -- used by the
        reconciliation sweep (PHASE4_PLAN.md section 15), not any user-facing
        listing (that is `list_unreviewed`, scoped to `unreviewed` only).
        """
        stmt = self._select(MergeRow).where(MergeRow.status == status).limit(limit)
        result = await self._session.execute(stmt)
        return [_row_to_dict(row) for row in result.scalars()]

    async def count_unreviewed(self) -> int:
        stmt = self._select_columns(MergeRow, func.count()).where(MergeRow.status == "unreviewed")
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def get(self, *, candidate_id: str) -> dict[str, Any] | None:
        stmt = self._select(MergeRow).where(MergeRow.id == candidate_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_dict(row) if row is not None else None

    async def try_mark_reviewed(
        self,
        *,
        candidate_id: str,
        if_version: int,
        new_status: str,
        expected_current_status: str,
    ) -> dict[str, Any] | None:
        """Optimistic-concurrency status transition, gated on both the version
        AND the current status (unlike the memory-review counterpart, this one
        also drives entity-merge reversal: `merged` -> `reversed` is legal,
        `unreviewed` -> `reversed` is not, so the expected starting state must
        be checked here too, not just left to the caller). Returns `None` on
        either mismatch -- a stale version and an illegal transition are both
        "the row is not in the state the caller assumed", handled the same way.
        """
        stmt = (
            self._update(MergeRow)
            .where(
                MergeRow.id == candidate_id,
                MergeRow.version == if_version,
                MergeRow.status == expected_current_status,
            )
            .values(status=new_status, version=MergeRow.version + 1)
            .returning(MergeRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_dict(row) if row is not None else None


def _row_to_dict(row: MergeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "entity_id_a": row.entity_id_a,
        "entity_id_b": row.entity_id_b,
        "evidence": row.evidence,
        "score": float(row.score) if row.score is not None else None,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at,
    }
