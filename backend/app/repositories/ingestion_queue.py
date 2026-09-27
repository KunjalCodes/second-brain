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

from app.db.models import Document as DocumentRow
from app.db.models import IngestionJob as JobRow
from app.models.document import DocumentStatus, IngestionJob, IngestionJobStatus
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

    def _owned(self, job_id: str, owner: str | None):  # noqa: ANN202
        """Conditional-UPDATE ownership check -- see `ExtractionQueue._owned`."""
        clause = JobRow.id == job_id
        if owner is not None:
            clause = and_(
                clause,
                JobRow.status == IngestionJobStatus.IN_PROGRESS.value,
                JobRow.locked_by == owner,
            )
        return clause

    async def renew_lease(self, *, job_id: str, owner: str, now: datetime) -> bool:
        result = await self._session.execute(
            update(JobRow).where(self._owned(job_id, owner)).values(locked_at=now)
        )
        return (result.rowcount or 0) == 1

    async def mark_done(self, *, job_id: str, now: datetime, owner: str | None = None) -> bool:
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(
                status=IngestionJobStatus.DONE.value,
                completed_at=now,
                error_code=None,
                error_detail=None,
                next_attempt_at=None,
                locked_at=None,
                locked_by=None,
            )
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def mark_failed(
        self,
        *,
        job_id: str,
        error_code: str,
        error_detail: str,
        permanent: bool,
        next_attempt_at: datetime | None,
        now: datetime,
        owner: str | None = None,
    ) -> bool:
        status = IngestionJobStatus.ABANDONED if permanent else IngestionJobStatus.FAILED
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(
                status=status.value,
                error_code=error_code,
                error_detail=error_detail[:500],
                next_attempt_at=None if permanent else next_attempt_at,
                completed_at=now if permanent else None,
                locked_at=None,
                locked_by=None,
            )
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def defer(
        self, *, job_id: str, owner: str, until: datetime, error_code: str, error_detail: str
    ) -> bool:
        """Back to `failed` without consuming an attempt -- see `ExtractionQueue.defer`."""
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(
                status=IngestionJobStatus.FAILED.value,
                attempts=JobRow.attempts - 1,
                error_code=error_code,
                error_detail=error_detail[:500],
                next_attempt_at=until,
                locked_at=None,
                locked_by=None,
            )
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def abandon_exhausted(self, *, max_attempts: int, now: datetime) -> list[str]:
        """`failed` with no attempts left -> `abandoned` (Phase 11, J5).
        Returns the affected jobs' document ids so their documents can be
        marked FAILED."""
        stmt = (
            update(JobRow)
            .where(
                JobRow.status == IngestionJobStatus.FAILED.value,
                JobRow.attempts >= max_attempts,
            )
            .values(
                status=IngestionJobStatus.ABANDONED.value,
                completed_at=now,
                next_attempt_at=None,
            )
            .returning(JobRow.document_id)
        )
        result = await self._session.execute(stmt)
        return [row[0] for row in result]

    async def fail_documents_of_abandoned_jobs(self) -> int:
        """A document whose ingestion job is `abandoned` must not stay in an
        in-flight status forever (Phase 11, J2/J5): it becomes FAILED with the
        job's last error code. Idempotent."""
        in_flight = (
            DocumentStatus.UPLOADED.value,
            DocumentStatus.VALIDATING.value,
            DocumentStatus.PARSING.value,
            DocumentStatus.CHUNKING.value,
            DocumentStatus.CHUNKED.value,
        )
        abandoned = select(JobRow.document_id).where(
            JobRow.status == IngestionJobStatus.ABANDONED.value
        )
        error_code = (
            select(JobRow.error_code)
            .where(JobRow.document_id == DocumentRow.id)
            .scalar_subquery()
        )
        result = await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id.in_(abandoned), DocumentRow.status.in_(in_flight))
            .values(
                status=DocumentStatus.FAILED.value,
                error_code=error_code,
                error_detail="Processing did not complete; upload the file again",
            )
        )
        return result.rowcount or 0
