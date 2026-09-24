"""Ingestion job creation (Phase 6, PHASE6_PLAN.md section 2/3) --
`UserScopedRepository`, mirroring `ExtractionJobRepository`'s own split: job
*creation* happens from the live request path with a known requesting user
and lives here; the cross-user queue operations (claim, reclaim, mark done/
failed) live in `app/repositories/ingestion_queue.py`, mirroring
`app/repositories/extraction_queue.py`'s exact, narrowly-scoped exemption.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert

from app.db.models import IngestionJob as JobRow
from app.models.document import IngestionJob, IngestionJobStatus
from app.repositories.sql_base import UserScopedRepository


def _job_to_domain(row: JobRow) -> IngestionJob:
    return IngestionJob(
        id=row.id,
        user_id=row.user_id,
        document_id=row.document_id,
        status=IngestionJobStatus(row.status),
        attempts=row.attempts,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        next_attempt_at=row.next_attempt_at,
        error_code=row.error_code,
        error_detail=row.error_detail,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


class IngestionJobRepository(UserScopedRepository):
    async def create_job(self, *, job_id: str, document_id: str, now: datetime) -> IngestionJob:
        """Insert a job, or return the existing one -- `ON CONFLICT
        (document_id) DO NOTHING` (only one ingestion job ever exists per
        document, `ingestion_jobs_one_per_document`).
        """
        stmt = (
            insert(JobRow)
            .values(
                id=job_id,
                user_id=self._user_id,
                document_id=document_id,
                status=IngestionJobStatus.PENDING.value,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["document_id"])
            .returning(JobRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _job_to_domain(row)

        stmt = self._select(JobRow).where(JobRow.document_id == document_id)
        result = await self._session.execute(stmt)
        return _job_to_domain(result.scalar_one())

    async def get(self, *, job_id: str) -> IngestionJob | None:
        row = await self._session.get(JobRow, job_id)
        if row is None or row.user_id != self._user_id:
            return None
        return _job_to_domain(row)
