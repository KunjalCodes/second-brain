"""Schemas for the review workflow (ADR 0010, PHASE4_PLAN.md section 6): both
the read-only list/detail views and the write-action request bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.models.domain import EntityDetail, MemoryDetail


class MemoryReviewCandidateSummary(BaseModel):
    """One row of the review queue -- cheap enough to list many of."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    memory_id_a: str
    memory_id_b: str
    score: float | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class MemoryReviewCandidateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MemoryReviewCandidateSummary]
    total: int
    limit: int
    offset: int


class MemoryReviewCandidateDetail(BaseModel):
    """The summary plus everything a reviewer needs to actually decide: the raw
    evidence the decision engine recorded, and both memories in full -- not just
    their ids -- since deciding "is this really a duplicate" requires reading the
    content (PHASE4_PLAN.md section 6: "affected memories" must be preserved).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    score: float | None
    status: str
    version: int
    evidence: dict[str, Any]
    source_extraction_candidate_id: str | None
    created_at: datetime
    updated_at: datetime
    memory_a: MemoryDetail
    memory_b: MemoryDetail


class EntityMergeCandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    entity_id_a: str
    entity_id_b: str
    score: float | None
    status: str
    version: int
    created_at: datetime


class EntityMergeCandidateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EntityMergeCandidateSummary]
    total: int
    limit: int
    offset: int


class EntityMergeCandidateDetail(BaseModel):
    """`entity_b` (and, in principle, `entity_a`) is `None` when the flag names
    an entity that did not exist in Neo4j yet at flag time -- the candidate's
    own entity, not created until persistence runs after ambiguity detection
    (`app.extraction.runner._flag_ambiguous_entities`'s `pending:<name>`
    placeholder). `evidence` always carries the human-readable name either way,
    so a reviewer is never left with nothing to act on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    score: float | None
    status: str
    version: int
    evidence: dict[str, Any]
    created_at: datetime
    entity_a: EntityDetail | None
    entity_b: EntityDetail | None


class ReviewAuditLogEntry(BaseModel):
    """One append-only audit row (PHASE4_PLAN.md section 11a). Never itself
    editable -- a correction to a review decision is a new row, not an edit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    action: str
    target_type: str
    target_id: str
    previous_state: dict[str, Any]
    resulting_state: dict[str, Any]
    reason: str | None
    reviewer_id: str
    created_at: datetime


class ReviewAuditLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReviewAuditLogEntry]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Write-action requests. `if_version` is required on every one -- optimistic
# concurrency (PHASE4_PLAN.md section 15): the caller must name the version it
# last read, so a stale write is rejected (409) rather than silently applied.
# ---------------------------------------------------------------------------


class ConfirmDuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int
    reason: str | None = None


class ConfirmDistinctRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int
    reason: str | None = None


class ResolveConflictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int
    resolution: Literal["supersede", "keep_both", "reject"]
    #: Required (and must be one of the candidate's own two memories) when
    #: resolution='supersede'; ignored otherwise.
    winner_memory_id: str | None = None
    reason: str | None = None


class ApproveEntityMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int
    #: Must be one of the candidate's own entity_id_a/entity_id_b -- the
    #: reviewer picks which entity survives, never inferred automatically.
    canonical_entity_id: str
    reason: str | None = None


class RejectEntityMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int
    reason: str | None = None


class ReverseEntityMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    if_version: int
    reason: str | None = None


class ReviewReconciliationResult(BaseModel):
    """Counts from one reconciliation sweep run across a bounded page of users
    (PHASE4_PLAN.md section 15) -- observability, not an audit log.
    """

    model_config = ConfigDict(extra="forbid")

    users_processed: int
    memory_duplicates_checked: int
    memory_duplicates_repaired: int
    entity_merges_checked: int
    entity_merges_repaired: int
