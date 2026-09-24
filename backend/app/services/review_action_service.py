"""Write side of the review workflow (ADR 0010, PHASE4_PLAN.md sections
6/9/10/11/15).

## Ordering, concurrency and idempotency (binding design, not incidental)

For every action: **Neo4j canonical mutation first, PostgreSQL review-state
second, audit log third** -- all inside the mutation's own guarantees, with no
distributed transaction spanning the two databases (none is available; Neo4j
and PostgreSQL are independent systems here). Concretely, each action:

1. Reads the PostgreSQL candidate row. If it is not `unreviewed`, no Neo4j
   call is made at all -- either this is an idempotent repeat of an action
   that already fully applied (returned as success, no new audit row), or it
   contradicts an already-recorded different decision (raises `ConflictError`).
2. Only when the row is `unreviewed` does it perform the Neo4j write. Every
   Neo4j write this module calls (`MemoryRepository.confirm_duplicate`,
   `resolve_conflict_supersede`, `resolve_conflict_reject`,
   `EntityRepository.merge`/`reverse_merge`) is itself idempotent (`MERGE`, not
   `CREATE`), so re-running it after a crash before step 3 below is safe.
3. Compare-and-swap the PostgreSQL row's status via `if_version`
   (`try_mark_reviewed`). A stale version raises `ConflictError` naming the
   current version -- never a silent overwrite.
4. Append one `ReviewAuditLog` row in the same session/transaction as step 3.

**Known, accepted race window**: between step 1's read and step 2's Neo4j
write, a *different* concurrent action on the *same* candidate could commit
first (e.g. another reviewer's CONFIRM_DISTINCT lands between this
CONFIRM_DUPLICATE's read and its Neo4j write). This action's own step-3 CAS
will then fail (the row is no longer `unreviewed`), so PostgreSQL never
records this action's outcome -- but the Neo4j write from step 2 may already
have committed. This is the documented crash/race window the plan calls for:
narrow (requires two contradictory actions on one candidate within
milliseconds), does not corrupt data (Neo4j's own history is never deleted,
only a MERGED-status/DUPLICATE_OF or SUPERSEDES edge is added), and is
detectable and repairable by a reconciliation sweep (comparing each
`confirmed_distinct` PostgreSQL row against Neo4j for an unexpected
DUPLICATE_OF edge) rather than by a distributed transaction, which this
project's stack does not have.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.review_audit_repository import ReviewAuditRepository
from app.schemas.review import EntityMergeCandidateDetail, MemoryReviewCandidateDetail
from app.services.review_service import ReviewService, resolve_pending_entity_id

#: Kinds CONFIRM_DUPLICATE/CONFIRM_DISTINCT apply to -- never 'conflicting',
#: which has its own, differently-shaped resolution actions below.
_DUPLICATE_REVIEW_KINDS = frozenset({"likely_duplicate", "flagged"})

ConflictResolution = Literal["supersede", "keep_both", "reject"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewActionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        review_service: ReviewService,
        memory_repository: MemoryRepository,
        entity_repository: EntityRepository,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._session = session
        self._reviews = review_service
        self._memories = memory_repository
        self._entities = entity_repository
        self._now = now_fn
        self._new_id = id_fn

    # -- memory review actions ------------------------------------------------

    async def confirm_duplicate(
        self, *, user_id: str, candidate_id: str, if_version: int, reason: str | None
    ) -> MemoryReviewCandidateDetail:
        repo = MemoryReviewCandidateRepository(self._session, user_id=user_id)
        row = await self._require_row(repo, candidate_id)
        if row["kind"] not in _DUPLICATE_REVIEW_KINDS:
            raise ValidationError(
                "CONFIRM_DUPLICATE only applies to likely_duplicate/flagged candidates",
                details={"kind": row["kind"]},
            )

        if row["status"] == "unreviewed":
            now = self._now()
            await self._memories.confirm_duplicate(
                user_id=user_id,
                duplicate_id=row["memory_id_a"],
                canonical_id=row["memory_id_b"],
                now=now,
            )
            await self._apply_memory_status(
                repo,
                row=row,
                if_version=if_version,
                new_status="confirmed_duplicate",
                action="CONFIRM_DUPLICATE",
                user_id=user_id,
                reason=reason,
                now=now,
            )
        else:
            self._require_idempotent_or_conflict(row, "confirmed_duplicate")

        return await self._reviews.get_memory_candidate(user_id=user_id, candidate_id=candidate_id)

    async def confirm_distinct(
        self, *, user_id: str, candidate_id: str, if_version: int, reason: str | None
    ) -> MemoryReviewCandidateDetail:
        """The suspected duplicate/flag is rejected: no Neo4j mutation at all,
        since LIKELY_DUPLICATE/FLAGGED never mutated Neo4j in the first place
        (ADR 0009) -- there is nothing to undo, only a decision to record.
        """
        repo = MemoryReviewCandidateRepository(self._session, user_id=user_id)
        row = await self._require_row(repo, candidate_id)
        if row["kind"] not in _DUPLICATE_REVIEW_KINDS:
            raise ValidationError(
                "CONFIRM_DISTINCT only applies to likely_duplicate/flagged candidates",
                details={"kind": row["kind"]},
            )

        if row["status"] == "unreviewed":
            now = self._now()
            await self._apply_memory_status(
                repo,
                row=row,
                if_version=if_version,
                new_status="confirmed_distinct",
                action="CONFIRM_DISTINCT",
                user_id=user_id,
                reason=reason,
                now=now,
            )
        else:
            self._require_idempotent_or_conflict(row, "confirmed_distinct")

        return await self._reviews.get_memory_candidate(user_id=user_id, candidate_id=candidate_id)

    async def resolve_conflict(
        self,
        *,
        user_id: str,
        candidate_id: str,
        if_version: int,
        resolution: ConflictResolution,
        winner_memory_id: str | None,
        reason: str | None,
    ) -> MemoryReviewCandidateDetail:
        """`resolution='supersede'` requires `winner_memory_id` (one of the
        candidate's own `memory_id_a`/`memory_id_b`) -- the reviewer, not the
        system, decides which fact wins; `'keep_both'` and `'reject'` take no
        side. The CONFLICTS_WITH edge itself is never removed either way (see
        `app.repositories.memory_repository`'s resolve_conflict_* docstrings):
        the fact that a conflict was once flagged is itself part of the
        history this system exists to keep.
        """
        repo = MemoryReviewCandidateRepository(self._session, user_id=user_id)
        row = await self._require_row(repo, candidate_id)
        if row["kind"] != "conflicting":
            raise ValidationError(
                "resolve_conflict only applies to conflicting candidates",
                details={"kind": row["kind"]},
            )

        action = {
            "supersede": "RESOLVE_CONFLICT_SUPERSEDE",
            "keep_both": "RESOLVE_CONFLICT_KEEP_BOTH",
            "reject": "RESOLVE_CONFLICT_REJECT",
        }[resolution]

        if row["status"] == "unreviewed":
            now = self._now()
            if resolution == "supersede":
                pair = {row["memory_id_a"], row["memory_id_b"]}
                if winner_memory_id not in pair:
                    raise ValidationError(
                        "winner_memory_id must be one of this candidate's two memories",
                        details={"winner_memory_id": winner_memory_id, "candidates": sorted(pair)},
                    )
                loser_memory_id = next(iter(pair - {winner_memory_id}))
                await self._memories.resolve_conflict_supersede(
                    user_id=user_id, winner_id=winner_memory_id, loser_id=loser_memory_id, now=now
                )
            elif resolution == "reject":
                # Both memories in a CONFLICTING pair keep whatever status they
                # already have except the one the decision engine itself
                # flagged CONFLICTING (memory_id_a, per its own docstring) --
                # see app.extraction.runner._persist: only the newer memory is
                # ever marked CONFLICTING, the existing one is left ACTIVE.
                await self._memories.resolve_conflict_reject(
                    user_id=user_id, memory_id=row["memory_id_a"], now=now
                )
            # 'keep_both': no Neo4j mutation -- both memories are left exactly
            # as they are; the conflict is acknowledged, not eliminated.

            await self._apply_memory_status(
                repo,
                row=row,
                if_version=if_version,
                new_status="conflict_resolved",
                action=action,
                user_id=user_id,
                reason=reason,
                now=now,
                resulting_extra={"resolution": resolution, "winner_memory_id": winner_memory_id},
            )
        else:
            self._require_idempotent_or_conflict(row, "conflict_resolved")

        return await self._reviews.get_memory_candidate(user_id=user_id, candidate_id=candidate_id)

    # -- entity merge actions ---------------------------------------------------

    async def approve_entity_merge(
        self,
        *,
        user_id: str,
        candidate_id: str,
        if_version: int,
        canonical_entity_id: str,
        reason: str | None,
    ) -> EntityMergeCandidateDetail:
        repo = EntityMergeCandidateRepository(self._session, user_id=user_id)
        row = await self._require_entity_row(repo, candidate_id)

        if row["status"] == "unreviewed":
            # Resolve any `pending:<name>` placeholder to the real entity
            # Neo4j created since this pair was flagged (the common case by
            # review time) -- see resolve_pending_entity_id's docstring.
            entity_id_a = await resolve_pending_entity_id(
                self._entities, user_id=user_id, entity_id=row["entity_id_a"]
            )
            entity_id_b = await resolve_pending_entity_id(
                self._entities, user_id=user_id, entity_id=row["entity_id_b"]
            )
            if entity_id_a is None or entity_id_b is None:
                raise ConflictError(
                    "One of the flagged entities does not exist yet -- it cannot "
                    "be merged until it is",
                    details={"entity_id_a": row["entity_id_a"], "entity_id_b": row["entity_id_b"]},
                )
            pair = {entity_id_a, entity_id_b}
            if canonical_entity_id not in pair:
                raise ValidationError(
                    "canonical_entity_id must be one of this candidate's two entities",
                    details={
                        "canonical_entity_id": canonical_entity_id,
                        "candidates": sorted(pair),
                    },
                )
            merged_entity_id = next(iter(pair - {canonical_entity_id}))

            now = self._now()
            merge_id = self._new_id()
            merged_entity = await self._entities.get(user_id=user_id, entity_id=merged_entity_id)
            new_aliases = [merged_entity.entity.name] if merged_entity is not None else []
            await self._entities.merge(
                user_id=user_id,
                entity_id_a=canonical_entity_id,
                entity_id_b=merged_entity_id,
                merge_id=merge_id,
                new_aliases=new_aliases,
                excluded_statuses=[],
                now=now,
                id_fn=self._new_id,
            )
            await self._apply_entity_status(
                repo,
                row=row,
                if_version=if_version,
                new_status="merged",
                expected_current_status="unreviewed",
                action="MERGE_ENTITY",
                user_id=user_id,
                reason=reason,
                now=now,
                resulting_extra={
                    "merge_id": merge_id,
                    "canonical_entity_id": canonical_entity_id,
                    "merged_entity_id": merged_entity_id,
                },
            )
        else:
            self._require_idempotent_or_conflict(row, "merged")

        return await self._reviews.get_entity_candidate(user_id=user_id, candidate_id=candidate_id)

    async def reject_entity_merge(
        self, *, user_id: str, candidate_id: str, if_version: int, reason: str | None
    ) -> EntityMergeCandidateDetail:
        """Both entities are left exactly as they are -- rejecting a merge
        candidate is a pure PostgreSQL state change, never a Neo4j mutation
        (nothing was ever merged to undo).
        """
        repo = EntityMergeCandidateRepository(self._session, user_id=user_id)
        row = await self._require_entity_row(repo, candidate_id)

        if row["status"] == "unreviewed":
            now = self._now()
            await self._apply_entity_status(
                repo,
                row=row,
                if_version=if_version,
                new_status="rejected",
                expected_current_status="unreviewed",
                action="REJECT_ENTITY_MERGE",
                user_id=user_id,
                reason=reason,
                now=now,
            )
        else:
            self._require_idempotent_or_conflict(row, "rejected")

        return await self._reviews.get_entity_candidate(user_id=user_id, candidate_id=candidate_id)

    async def reverse_entity_merge(
        self, *, user_id: str, candidate_id: str, if_version: int, reason: str | None
    ) -> EntityMergeCandidateDetail:
        """Undo a previously-approved merge: the copied relationships and the
        redirect are removed (`EntityRepository.reverse_merge`), the merged
        entity's aliases are NOT retracted (documented, accepted limitation --
        see that method's docstring). Requires the candidate to currently be
        `merged`; anything else is a `ConflictError`, not silently accepted.
        """
        repo = EntityMergeCandidateRepository(self._session, user_id=user_id)
        row = await self._require_entity_row(repo, candidate_id)

        if row["status"] == "merged":
            now = self._now()
            audit_repo = ReviewAuditRepository(self._session, user_id=user_id)
            merge_info = await self._find_merge_audit_entry(audit_repo, candidate_id)
            if merge_info is None:
                # No MERGE_ENTITY audit entry for this candidate -- should be
                # unreachable (every approve_entity_merge writes one before
                # this status is ever reachable), but fail loudly rather than
                # reverse an unknown, untagged set of edges.
                raise ConflictError(
                    "This merge cannot be reversed: no merge audit record was found",
                    details={"candidate_id": candidate_id},
                )
            merge_id = merge_info["merge_id"]
            canonical_entity_id = merge_info["canonical_entity_id"]
            merged_entity_id = merge_info["merged_entity_id"]
            await self._entities.reverse_merge(
                user_id=user_id,
                entity_id_a=canonical_entity_id,
                entity_id_b=merged_entity_id,
                merge_id=merge_id,
            )
            await self._apply_entity_status(
                repo,
                row=row,
                if_version=if_version,
                new_status="reversed",
                expected_current_status="merged",
                action="REVERSE_MERGE",
                user_id=user_id,
                reason=reason,
                now=now,
            )
        else:
            self._require_idempotent_or_conflict(row, "reversed")

        return await self._reviews.get_entity_candidate(user_id=user_id, candidate_id=candidate_id)

    # -- shared plumbing ----------------------------------------------------

    async def _require_row(
        self, repo: MemoryReviewCandidateRepository, candidate_id: str
    ) -> dict[str, Any]:
        row = await repo.get(candidate_id=candidate_id)
        if row is None:
            raise NotFoundError("Review candidate not found", details={"id": candidate_id})
        return row

    async def _require_entity_row(
        self, repo: EntityMergeCandidateRepository, candidate_id: str
    ) -> dict[str, Any]:
        row = await repo.get(candidate_id=candidate_id)
        if row is None:
            raise NotFoundError("Review candidate not found", details={"id": candidate_id})
        return row

    def _require_idempotent_or_conflict(self, row: dict[str, Any], expected_status: str) -> None:
        """The row is already reviewed. Success (no-op) if it already carries
        exactly the outcome this action would have produced; otherwise the
        candidate was resolved differently, which is a conflict, not a retry.
        """
        if row["status"] != expected_status:
            raise ConflictError(
                "This review candidate was already resolved differently",
                details={"current_status": row["status"], "expected_status": expected_status},
            )

    async def _apply_memory_status(
        self,
        repo: MemoryReviewCandidateRepository,
        *,
        row: dict[str, Any],
        if_version: int,
        new_status: str,
        action: str,
        user_id: str,
        reason: str | None,
        now: datetime,
        resulting_extra: dict[str, Any] | None = None,
    ) -> None:
        updated = await repo.try_mark_reviewed(
            candidate_id=row["id"], if_version=if_version, new_status=new_status, now=now
        )
        if updated is None:
            current = await repo.get(candidate_id=row["id"])
            if current is None:
                raise NotFoundError("Review candidate not found", details={"id": row["id"]})
            if current["status"] == new_status:
                return  # idempotent: someone else's identical action already won the race
            if current["version"] != if_version:
                raise ConflictError(
                    "Stale version -- re-read the candidate and retry",
                    details={"expected_version": if_version, "current_version": current["version"]},
                )
            raise ConflictError(
                "This review candidate was already resolved differently",
                details={"current_status": current["status"]},
            )
        audit_repo = ReviewAuditRepository(self._session, user_id=user_id)
        await audit_repo.record(
            entry_id=self._new_id(),
            action=action,
            target_type="memory_review_candidate",
            target_id=row["id"],
            previous_state={"status": "unreviewed", "version": if_version},
            resulting_state={
                "status": new_status,
                "version": updated["version"],
                **(resulting_extra or {}),
            },
            reason=reason,
            reviewer_id=user_id,
            now=now,
        )

    async def _apply_entity_status(
        self,
        repo: EntityMergeCandidateRepository,
        *,
        row: dict[str, Any],
        if_version: int,
        new_status: str,
        expected_current_status: str,
        action: str,
        user_id: str,
        reason: str | None,
        now: datetime,
        resulting_extra: dict[str, Any] | None = None,
    ) -> None:
        updated = await repo.try_mark_reviewed(
            candidate_id=row["id"],
            if_version=if_version,
            new_status=new_status,
            expected_current_status=expected_current_status,
        )
        if updated is None:
            current = await repo.get(candidate_id=row["id"])
            if current is None:
                raise NotFoundError("Review candidate not found", details={"id": row["id"]})
            if current["status"] == new_status:
                return
            if current["version"] != if_version:
                raise ConflictError(
                    "Stale version -- re-read the candidate and retry",
                    details={"expected_version": if_version, "current_version": current["version"]},
                )
            raise ConflictError(
                "This review candidate was already resolved differently",
                details={"current_status": current["status"]},
            )
        audit_repo = ReviewAuditRepository(self._session, user_id=user_id)
        await audit_repo.record(
            entry_id=self._new_id(),
            action=action,
            target_type="entity_merge_candidate",
            target_id=row["id"],
            previous_state={"status": expected_current_status, "version": if_version},
            resulting_state={
                "status": new_status,
                "version": updated["version"],
                **(resulting_extra or {}),
            },
            reason=reason,
            reviewer_id=user_id,
            now=now,
        )

    async def _find_merge_audit_entry(
        self, audit_repo: ReviewAuditRepository, candidate_id: str
    ) -> dict[str, Any] | None:
        """The `MERGE_ENTITY` audit entry for this candidate, if any -- the
        audit log (never overwritten) is the source of truth for which entity
        was picked canonical and what `merge_id` tagged the copied edges, so
        `reverse_entity_merge` never needs to mutate the candidate's own
        `evidence` (which stays exactly what the decision engine flagged).
        """
        entries = await audit_repo.list_for_target(
            target_type="entity_merge_candidate", target_id=candidate_id
        )
        for entry in reversed(entries):  # most recent MERGE_ENTITY wins
            if entry["action"] == "MERGE_ENTITY":
                return entry["resulting_state"]
        return None
