"""Review workflow endpoints (ADR 0010, PHASE4_PLAN.md sections 6/9/10/11/12).

List/detail/history are pure reads; the action endpoints
(confirm/reject/resolve/merge/reverse) mutate through `ReviewActionService`,
which owns the Neo4j-then-PostgreSQL-then-audit ordering and the optimistic-
concurrency contract (see that module's docstring). Every handler resolves
`user_id` from `CurrentUser` and every underlying repository call is
user-scoped -- there is no path here, or anywhere else in this codebase, for
one user to act on another's review candidates.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, InternalTokenDep, ReviewActionServiceDep, ReviewServiceDep
from app.db.engine import get_database
from app.graph.connection import get_graph_client
from app.repositories.user_repository import UserRepository
from app.schemas.review import (
    ApproveEntityMergeRequest,
    ConfirmDistinctRequest,
    ConfirmDuplicateRequest,
    EntityMergeCandidateDetail,
    EntityMergeCandidateListResponse,
    MemoryReviewCandidateDetail,
    MemoryReviewCandidateListResponse,
    RejectEntityMergeRequest,
    ResolveConflictRequest,
    ReverseEntityMergeRequest,
    ReviewAuditLogResponse,
    ReviewReconciliationResult,
)
from app.services.review_reconciliation_service import run_reconciliation_tick

router = APIRouter(prefix="/review", tags=["review"])
internal_router = APIRouter(prefix="/internal/review", tags=["internal"])

#: Bounded page of users processed per call -- same shape as the embedding
#: backfill sweep (app/api/routes/embeddings.py): Neo4j's $user_id scoping
#: means there is no single "all users" reconciliation query.
_DEFAULT_MAX_USERS = 100


@internal_router.post(
    "/reconcile",
    response_model=ReviewReconciliationResult,
    summary="Run one bounded review reconciliation pass (internal; requires X-Internal-Token)",
)
async def trigger_reconciliation(
    _: InternalTokenDep,
    max_users: Annotated[int, Query(ge=1, le=1000)] = _DEFAULT_MAX_USERS,
) -> ReviewReconciliationResult:
    """Re-check every `confirmed_duplicate`/`merged` review row against Neo4j's
    actual current state, repairing the narrow crash/race window
    `app.services.review_action_service` documents by re-applying the same
    idempotent Neo4j write -- never a freshly-guessed outcome.
    """
    db = get_database()
    graph = get_graph_client()

    async with db.session() as session:
        user_ids = await UserRepository(session).list_ids(limit=max_users)

    totals = {
        "memory_duplicates_checked": 0,
        "memory_duplicates_repaired": 0,
        "entity_merges_checked": 0,
        "entity_merges_repaired": 0,
    }
    for user_id in user_ids:
        counts = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        for key in totals:
            totals[key] += counts[key]

    return ReviewReconciliationResult(users_processed=len(user_ids), **totals)


@router.get(
    "/memories",
    response_model=MemoryReviewCandidateListResponse,
    summary="List pending memory review candidates (duplicate/flagged/conflicting)",
)
async def list_memory_review_candidates(
    user_id: CurrentUser,
    service: ReviewServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryReviewCandidateListResponse:
    return await service.list_memory_candidates(user_id=user_id, limit=limit, offset=offset)


@router.get(
    "/memories/{candidate_id}",
    response_model=MemoryReviewCandidateDetail,
    summary="Get one memory review candidate, with both memories in full",
)
async def get_memory_review_candidate(
    candidate_id: str,
    user_id: CurrentUser,
    service: ReviewServiceDep,
) -> MemoryReviewCandidateDetail:
    return await service.get_memory_candidate(user_id=user_id, candidate_id=candidate_id)


@router.get(
    "/entities",
    response_model=EntityMergeCandidateListResponse,
    summary="List pending entity merge candidates",
)
async def list_entity_review_candidates(
    user_id: CurrentUser,
    service: ReviewServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntityMergeCandidateListResponse:
    return await service.list_entity_candidates(user_id=user_id, limit=limit, offset=offset)


@router.get(
    "/entities/{candidate_id}",
    response_model=EntityMergeCandidateDetail,
    summary="Get one entity merge candidate, with both entities in full",
)
async def get_entity_review_candidate(
    candidate_id: str,
    user_id: CurrentUser,
    service: ReviewServiceDep,
) -> EntityMergeCandidateDetail:
    return await service.get_entity_candidate(user_id=user_id, candidate_id=candidate_id)


@router.get(
    "/history",
    response_model=ReviewAuditLogResponse,
    summary="List past review decisions (append-only audit trail)",
)
async def list_review_history(
    user_id: CurrentUser,
    service: ReviewServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewAuditLogResponse:
    return await service.list_audit_log(user_id=user_id, limit=limit, offset=offset)


@router.post(
    "/memories/{candidate_id}/confirm-duplicate",
    response_model=MemoryReviewCandidateDetail,
    summary="Confirm a likely-duplicate/flagged candidate as a true duplicate",
)
async def confirm_duplicate(
    candidate_id: str,
    payload: ConfirmDuplicateRequest,
    user_id: CurrentUser,
    service: ReviewActionServiceDep,
) -> MemoryReviewCandidateDetail:
    """`memory_id_a` (the newer memory) is marked MERGED with a DUPLICATE_OF
    edge to `memory_id_b` (kept as canonical). Neither memory is deleted.
    """
    return await service.confirm_duplicate(
        user_id=user_id,
        candidate_id=candidate_id,
        if_version=payload.if_version,
        reason=payload.reason,
    )


@router.post(
    "/memories/{candidate_id}/confirm-distinct",
    response_model=MemoryReviewCandidateDetail,
    summary="Reject a likely-duplicate/flagged candidate as not actually a duplicate",
)
async def confirm_distinct(
    candidate_id: str,
    payload: ConfirmDistinctRequest,
    user_id: CurrentUser,
    service: ReviewActionServiceDep,
) -> MemoryReviewCandidateDetail:
    return await service.confirm_distinct(
        user_id=user_id,
        candidate_id=candidate_id,
        if_version=payload.if_version,
        reason=payload.reason,
    )


@router.post(
    "/memories/{candidate_id}/resolve-conflict",
    response_model=MemoryReviewCandidateDetail,
    summary="Resolve a conflicting memory pair (supersede / keep both / reject the flag)",
)
async def resolve_conflict(
    candidate_id: str,
    payload: ResolveConflictRequest,
    user_id: CurrentUser,
    service: ReviewActionServiceDep,
) -> MemoryReviewCandidateDetail:
    return await service.resolve_conflict(
        user_id=user_id,
        candidate_id=candidate_id,
        if_version=payload.if_version,
        resolution=payload.resolution,
        winner_memory_id=payload.winner_memory_id,
        reason=payload.reason,
    )


@router.post(
    "/entities/{candidate_id}/approve-merge",
    response_model=EntityMergeCandidateDetail,
    summary="Approve an entity merge candidate",
)
async def approve_entity_merge(
    candidate_id: str,
    payload: ApproveEntityMergeRequest,
    user_id: CurrentUser,
    service: ReviewActionServiceDep,
) -> EntityMergeCandidateDetail:
    """The non-canonical entity is never deleted: its relationships are copied
    (not moved) onto the canonical entity, and a MERGED_INTO redirect is
    created so lookups by its id resolve to the canonical one.
    """
    return await service.approve_entity_merge(
        user_id=user_id,
        candidate_id=candidate_id,
        if_version=payload.if_version,
        canonical_entity_id=payload.canonical_entity_id,
        reason=payload.reason,
    )


@router.post(
    "/entities/{candidate_id}/reject-merge",
    response_model=EntityMergeCandidateDetail,
    summary="Reject an entity merge candidate (both entities stay distinct)",
)
async def reject_entity_merge(
    candidate_id: str,
    payload: RejectEntityMergeRequest,
    user_id: CurrentUser,
    service: ReviewActionServiceDep,
) -> EntityMergeCandidateDetail:
    return await service.reject_entity_merge(
        user_id=user_id,
        candidate_id=candidate_id,
        if_version=payload.if_version,
        reason=payload.reason,
    )


@router.post(
    "/entities/{candidate_id}/reverse-merge",
    response_model=EntityMergeCandidateDetail,
    summary="Reverse a previously-approved entity merge",
)
async def reverse_entity_merge(
    candidate_id: str,
    payload: ReverseEntityMergeRequest,
    user_id: CurrentUser,
    service: ReviewActionServiceDep,
) -> EntityMergeCandidateDetail:
    return await service.reverse_entity_merge(
        user_id=user_id,
        candidate_id=candidate_id,
        if_version=payload.if_version,
        reason=payload.reason,
    )
