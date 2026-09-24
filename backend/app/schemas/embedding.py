"""Request/response schemas for the embedding backfill endpoint (ADR 0009)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EmbeddingBackfillResult(BaseModel):
    """Counts from one backfill sweep run across a bounded page of users --
    observability, not an audit log (mirrors `SweepResult`).
    """

    model_config = ConfigDict(extra="forbid")

    users_processed: int
    scanned: int
    embedded: int
    skipped_already_present: int
    failed: int
