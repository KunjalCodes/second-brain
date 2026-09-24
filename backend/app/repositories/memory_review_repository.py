"""Persistence for the memory review workflow (ADR 0010, PHASE4_PLAN.md section 6).

Sibling to `entity_merge_repository.py`: the decision engine only ever *flags* a
`LIKELY_DUPLICATE`/`FLAGGED`/`CONFLICTING` pair here -- it never confirms, merges,
or rejects anything. That remains an explicit, later, human/application decision
(the review write paths of later Phase 4 steps).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app.db.models import MemoryReviewCandidate as ReviewRow
from app.repositories.sql_base import UserScopedRepository


class MemoryReviewCandidateRepository(UserScopedRepository):
    async def record_candidate(
        self,
        *,
        candidate_id: str,
        kind: str,
        memory_id_a: str,
        memory_id_b: str,
        source_extraction_candidate_id: str | None,
        evidence: dict[str, Any],
        score: float | None,
        now: datetime,
    ) -> None:
        """Flag one (memory_id_a, memory_id_b, kind) pair for review.

        `memory_id_a` is the newer/candidate memory, `memory_id_b` the existing
        one it was compared against -- never sorted, unlike the entity-merge
        pair, because the model's own docstring assigns that direction meaning.
        `ON CONFLICT DO NOTHING` on the row's unique triple makes a repeated
        extraction attempt (idempotency layer 2, docs/PHASE2_HARDENING.md) not
        duplicate the flag; the row already there is left exactly as a reviewer
        may have already left it.
        """
        stmt = (
            insert(ReviewRow)
            .values(
                id=candidate_id,
                user_id=self._user_id,
                kind=kind,
                memory_id_a=memory_id_a,
                memory_id_b=memory_id_b,
                source_extraction_candidate_id=source_extraction_candidate_id,
                evidence=evidence,
                score=score,
                status="unreviewed",
                version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "memory_id_a", "memory_id_b", "kind"]
            )
        )
        await self._session.execute(stmt)

    async def list_unreviewed(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        stmt = (
            self._select(ReviewRow)
            .where(ReviewRow.status == "unreviewed")
            .order_by(ReviewRow.created_at.desc())
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
        stmt = self._select(ReviewRow).where(ReviewRow.status == status).limit(limit)
        result = await self._session.execute(stmt)
        return [_row_to_dict(row) for row in result.scalars()]

    async def count_unreviewed(self) -> int:
        stmt = self._select_columns(ReviewRow, func.count()).where(ReviewRow.status == "unreviewed")
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def get(self, *, candidate_id: str) -> dict[str, Any] | None:
        stmt = self._select(ReviewRow).where(ReviewRow.id == candidate_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_dict(row) if row is not None else None

    async def try_mark_reviewed(
        self, *, candidate_id: str, if_version: int, new_status: str, now: datetime
    ) -> dict[str, Any] | None:
        """Optimistic-concurrency status transition (PHASE4_PLAN.md section 15).

        `WHERE id = :id AND version = :if_version AND status = 'unreviewed'` is
        the compare-and-swap: the `UPDATE` touches a row only if nobody has
        changed it since the caller last read it, AND it is still pending (this
        table has no reversal path, unlike entity-merge candidates, so any
        non-`unreviewed` row is always an illegal target regardless of
        version). Returns the updated row, or `None` on either mismatch -- a
        stale version and an already-reviewed row are both "the row is not in
        the state the caller assumed", handled the same way rather than this
        method raising: the caller re-reads to tell them apart.
        """
        stmt = (
            self._update(ReviewRow)
            .where(
                ReviewRow.id == candidate_id,
                ReviewRow.version == if_version,
                ReviewRow.status == "unreviewed",
            )
            .values(status=new_status, version=ReviewRow.version + 1, updated_at=now)
            .returning(ReviewRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_dict(row) if row is not None else None


def _row_to_dict(row: ReviewRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "memory_id_a": row.memory_id_a,
        "memory_id_b": row.memory_id_b,
        "source_extraction_candidate_id": row.source_extraction_candidate_id,
        "evidence": row.evidence,
        "score": float(row.score) if row.score is not None else None,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
