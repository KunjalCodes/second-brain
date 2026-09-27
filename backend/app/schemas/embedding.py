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
    #: Users in this page whose every memory is now embedded (Phase 11).
    users_complete: int
    #: Pass as `user_offset` to continue with the next page of users; `None`
    #: when this page was the last.
    next_user_offset: int | None
