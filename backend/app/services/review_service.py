"""Read side of the review workflow (ADR 0010, PHASE4_PLAN.md section 6).

Enriches the raw PostgreSQL review-candidate rows with the actual Neo4j
memory/entity content a reviewer needs to decide -- a bare pair of ids is not
enough to judge "is this really a duplicate". Write actions (confirm/reject/
merge/reverse) are a separate, later service; this one never mutates anything.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.domain import EntityDetail
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.review_audit_repository import ReviewAuditRepository
from app.schemas.review import (
    EntityMergeCandidateDetail,
    EntityMergeCandidateListResponse,
    EntityMergeCandidateSummary,
    MemoryReviewCandidateDetail,
    MemoryReviewCandidateListResponse,
    MemoryReviewCandidateSummary,
    ReviewAuditLogEntry,
    ReviewAuditLogResponse,
)
from app.services.memory_service import MemoryService


def _summarize[ModelT: BaseModel](model: type[ModelT], row: dict[str, Any]) -> ModelT:
    """Project a repository row (which may carry extra fields a `...Detail`
    schema needs, e.g. `evidence`) onto a leaner `...Summary` schema's own
    declared fields -- every summary schema keeps `extra="forbid"`, so this is
    an explicit projection rather than loosening that safety net.
    """
    return model.model_validate({k: row[k] for k in model.model_fields if k in row})


_PENDING_PREFIX = "pending:"


async def resolve_pending_entity_id(
    entities: EntityRepository, *, user_id: str, entity_id: str
) -> str | None:
    """Resolve an `EntityMergeCandidate.entity_id_*` value to a real Neo4j id.

    `app.extraction.runner._flag_ambiguous_entities` stores
    `f"pending:{normalized_name}"` for the candidate's own entity when it does
    not exist in Neo4j yet at flag time (persistence runs afterward). By the
    time a candidate is reviewed, that entity has almost always been created
    since -- this looks it up by its normalized name rather than leaving the
    placeholder unresolved forever. Returns `None` if the id is a real id that
    no longer resolves, or a pending placeholder whose entity still does not
    exist (both read and write callers treat that the same way: nothing to
    show yet, or nothing to merge yet).
    """
    if not entity_id.startswith(_PENDING_PREFIX):
        return entity_id
    normalized_name = entity_id.removeprefix(_PENDING_PREFIX)
    entity = await entities.find_by_normalized_name(
        user_id=user_id, normalized_name=normalized_name
    )
    return entity.id if entity is not None else None


class ReviewService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        memory_service: MemoryService,
        entity_repository: EntityRepository,
    ) -> None:
        self._session = session
        self._memory_service = memory_service
        self._entities = entity_repository

    # -- memory review candidates -------------------------------------------

    async def list_memory_candidates(
        self, *, user_id: str, limit: int, offset: int
    ) -> MemoryReviewCandidateListResponse:
        repo = MemoryReviewCandidateRepository(self._session, user_id=user_id)
        rows = await repo.list_unreviewed(limit=limit, offset=offset)
        total = await repo.count_unreviewed()
        return MemoryReviewCandidateListResponse(
            items=[_summarize(MemoryReviewCandidateSummary, row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_memory_candidate(
        self, *, user_id: str, candidate_id: str
    ) -> MemoryReviewCandidateDetail:
        repo = MemoryReviewCandidateRepository(self._session, user_id=user_id)
        row = await repo.get(candidate_id=candidate_id)
        if row is None:
            raise NotFoundError("Review candidate not found", details={"id": candidate_id})

        memory_a = await self._memory_service.get(user_id=user_id, memory_id=row["memory_id_a"])
        memory_b = await self._memory_service.get(user_id=user_id, memory_id=row["memory_id_b"])
        return MemoryReviewCandidateDetail(
            id=row["id"],
            kind=row["kind"],
            score=row["score"],
            status=row["status"],
            version=row["version"],
            evidence=row["evidence"],
            source_extraction_candidate_id=row["source_extraction_candidate_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            memory_a=memory_a,
            memory_b=memory_b,
        )

    # -- entity merge candidates ---------------------------------------------

    async def list_entity_candidates(
        self, *, user_id: str, limit: int, offset: int
    ) -> EntityMergeCandidateListResponse:
        repo = EntityMergeCandidateRepository(self._session, user_id=user_id)
        rows = await repo.list_unreviewed(limit=limit, offset=offset)
        total = await repo.count_unreviewed()
        return EntityMergeCandidateListResponse(
            items=[_summarize(EntityMergeCandidateSummary, row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_entity_candidate(
        self, *, user_id: str, candidate_id: str
    ) -> EntityMergeCandidateDetail:
        repo = EntityMergeCandidateRepository(self._session, user_id=user_id)
        row = await repo.get(candidate_id=candidate_id)
        if row is None:
            raise NotFoundError("Review candidate not found", details={"id": candidate_id})

        entity_a = await self._resolve_flagged_entity(user_id=user_id, entity_id=row["entity_id_a"])
        entity_b = await self._resolve_flagged_entity(user_id=user_id, entity_id=row["entity_id_b"])
        return EntityMergeCandidateDetail(
            id=row["id"],
            score=row["score"],
            status=row["status"],
            version=row["version"],
            evidence=row["evidence"],
            created_at=row["created_at"],
            entity_a=entity_a,
            entity_b=entity_b,
        )

    async def _resolve_flagged_entity(self, *, user_id: str, entity_id: str) -> EntityDetail | None:
        """`None` for a `pending:<name>` placeholder whose entity still does
        not exist, or for an id that genuinely no longer resolves -- either
        way, `evidence` on the parent response still carries the name, so a
        reviewer is never left with nothing to act on. A placeholder whose
        entity now exists (the common case by review time) resolves to it.
        """
        real_id = await resolve_pending_entity_id(
            self._entities, user_id=user_id, entity_id=entity_id
        )
        if real_id is None:
            return None
        return await self._entities.get(user_id=user_id, entity_id=real_id)

    # -- audit history --------------------------------------------------------

    async def list_audit_log(
        self, *, user_id: str, limit: int, offset: int
    ) -> ReviewAuditLogResponse:
        repo = ReviewAuditRepository(self._session, user_id=user_id)
        rows = await repo.list_recent(limit=limit, offset=offset)
        total = await repo.count()
        return ReviewAuditLogResponse(
            # Explicit mapping (Phase 11, M8): spreading repository rows into
            # this extra="forbid" model turns any future column into a 500.
            items=[
                ReviewAuditLogEntry(
                    id=row["id"],
                    action=row["action"],
                    target_type=row["target_type"],
                    target_id=row["target_id"],
                    previous_state=row["previous_state"],
                    resulting_state=row["resulting_state"],
                    reason=row["reason"],
                    reviewer_id=row["reviewer_id"],
                    created_at=row["created_at"],
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )
