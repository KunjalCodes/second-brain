"""Request/response schemas for the extraction status and sweep endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.extraction import ExtractionCandidateRecord, ExtractionJob


class ExtractionJobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExtractionJob]
    limit: int
    offset: int


class ExtractionJobDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: ExtractionJob
    candidates: list[ExtractionCandidateRecord]


class SweepResult(BaseModel):
    """Counts from one sweep tick (docs/adr/0008) -- observability, not an audit log."""

    model_config = ConfigDict(extra="forbid")

    reclaimed_leases: int
    rechecked_skipped: int
    jobs_processed: int
