"""Read-only access to extraction job/candidate state, for observability.

Thin by design: `ExtractionJobRepository` and `ExtractionCandidateRepository`
already enforce user scoping, so this service adds only the not-found translation
that keeps the API's error shape consistent with every other resource.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.extraction import ExtractionJob
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.schemas.extraction import ExtractionJobDetail, ExtractionJobListResponse


class ExtractionStatusService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_jobs(
        self, *, user_id: str, limit: int, offset: int
    ) -> ExtractionJobListResponse:
        repo = ExtractionJobRepository(self._session, user_id=user_id)
        items = await repo.list_recent(limit=limit, offset=offset)
        return ExtractionJobListResponse(items=items, limit=limit, offset=offset)

    async def get_job(self, *, user_id: str, job_id: str) -> ExtractionJobDetail:
        job_repo = ExtractionJobRepository(self._session, user_id=user_id)
        job: ExtractionJob | None = await job_repo.get(job_id=job_id)
        if job is None:
            raise NotFoundError("Extraction job not found", details={"job_id": job_id})
        candidates_repo = ExtractionCandidateRepository(self._session, user_id=user_id)
        candidates = await candidates_repo.list_for_job(extraction_job_id=job_id)
        return ExtractionJobDetail(job=job, candidates=candidates)
