"""The ingestion job queue: claim, lease, complete, fail (Phase 6,
PHASE6_PLAN.md section 2).

**Deliberately not a `UserScopedRepository`**, for the exact same reason
`app/repositories/extraction_queue.py` isn't: a worker tick processes jobs
across every user by design. Exempted from the `select`/`update`/`delete`
import ban in `tests/unit/test_sql_safety.py` the same way that file is.
Every job this module touches still carries its own `user_id` column, and
callers (`app/documents/ingestion.py`) are required to use *that* value --
taken from the claimed row, never from ambient request context -- for every
downstream write, exactly matching `ExtractionQueue`'s documented contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IngestionJob as JobRow
from app.models.document import IngestionJob, IngestionJobStatus
from app.repositories.ingestion_repository import _job_to_domain


class IngestionQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_batch(
        self, *, limit: int, worker_id: str, now: datetime, max_attempts: int
    ) -> list[IngestionJob]:
        """Atomically claim up to `limit` jobs, across all users -- the
        exact `SELECT ... FOR UPDATE SKIP LOCKED` + CTE pattern
        `ExtractionQueue.claim_batch` already uses (see that method's own
        docstring for why a CTE, not `WHERE id IN (subquery)`, is required
        for single evaluation).
        """
        claimable = (
            select(JobRow.id)
            .where(
                or_(
                    JobRow.status == IngestionJobStatus.PENDING.value,
                    and_(
                        JobRow.status == IngestionJobStatus.FAILED.value,
                        JobRow.attempts < max_attempts,
                        JobRow.next_attempt_at.is_not(None),
                        JobRow.next_attempt_at <= now,
                    ),
                )
            )
            .order_by(JobRow.created_at, JobRow.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("claimable")
        )
        stmt = (
            update(JobRow)
            .where(JobRow.id == claimable.c.id)
            .values(
                status=IngestionJobStatus.IN_PROGRESS.value,
                locked_at=now,
                locked_by=worker_id,
                attempts=JobRow.attempts + 1,
            )
            .returning(JobRow)
        )
        result = await self._session.execute(stmt)
        return [_job_to_domain(row) for row in result.scalars()]

    async def reclaim_expired_leases(
        self, *, lease_seconds: int, max_attempts: int, now: datetime
    ) -> int:
        """Recover jobs whose worker died mid-run -- mirrors
        `ExtractionQueue.reclaim_expired_leases` exactly.
        """
        cutoff = now - timedelta(seconds=lease_seconds)

        retryable = (
            update(JobRow)
            .where(
                JobRow.status == IngestionJobStatus.IN_PROGRESS.value,
                JobRow.locked_at < cutoff,
                JobRow.attempts < max_attempts,
            )
            .values(
                status=IngestionJobStatus.FAILED.value,
                next_attempt_at=now,
                error_code="lease_expired",
                error_detail="Worker did not complete the job before its lease expired",
                locked_at=None,
                locked_by=None,
            )
        )
        exhausted = (
            update(JobRow)
            .where(
                JobRow.status == IngestionJobStatus.IN_PROGRESS.value,
                JobRow.locked_at < cutoff,
                JobRow.attempts >= max_attempts,
            )
            .values(
                status=IngestionJobStatus.ABANDONED.value,
                completed_at=now,
                error_code="lease_expired",
                error_detail=(
                    "Worker did not complete the job before its lease expired, "
                    "and no attempts remain"
                ),
            )
        )
        retried = await self._session.execute(retryable)
        given_up = await self._session.execute(exhausted)
        return (retried.rowcount or 0) + (given_up.rowcount or 0)

    async def mark_done(self, *, job_id: str, now: datetime) -> None:
        stmt = (
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(
                status=IngestionJobStatus.DONE.value,
                completed_at=now,
                locked_at=None,
                locked_by=None,
            )
        )
        await self._session.execute(stmt)

    async def mark_failed(
        self,
        *,
        job_id: str,
        error_code: str,
        error_detail: str,
        permanent: bool,
        next_attempt_at: datetime | None,
        now: datetime,
    ) -> None:
        status = IngestionJobStatus.ABANDONED if permanent else IngestionJobStatus.FAILED
        stmt = (
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(
                status=status.value,
                error_code=error_code,
                error_detail=error_detail,
                next_attempt_at=None if permanent else next_attempt_at,
                completed_at=now if permanent else None,
                locked_at=None,
                locked_by=None,
            )
        )
        await self._session.execute(stmt)
