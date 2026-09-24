"""Cross-database reconciliation sweep (ADR 0010, PHASE4_PLAN.md sections
11/15) -- closes the crash window `app.services.review_action_service`
documents: Neo4j canonical mutation first, PostgreSQL review-state second, no
distributed transaction between them, so a crash between the two can leave
PostgreSQL recording a decision that Neo4j never actually applied.

This never GUESSES what happened -- it reads PostgreSQL's own terminal-status
rows (which decision was recorded) and checks Neo4j's actual current state
against it, repairing only by RE-APPLYING the same idempotent Neo4j write the
original action would have made (never inventing a different outcome). A row
this sweep cannot safely repair (e.g. a `merged` entity candidate with no
`MERGE_ENTITY` audit entry to read the chosen canonical/merge_id from) is
left alone and counted, not guessed at.

Mirrors `app.semantic.embedding_pipeline.run_embedding_backfill_tick`'s shape:
one bounded, resumable, per-user tick, intended to be called by the managed
host's own cron via an internal endpoint -- see
`POST /internal/review/reconcile`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.engine import Database
from app.graph.connection import GraphClient
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.review_audit_repository import ReviewAuditRepository

logger = logging.getLogger(__name__)

#: Bounded page of terminal-status rows scanned per user per tick -- mirrors
#: the embedding backfill sweep's own bounded-batch discipline.
_SCAN_LIMIT = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _reconcile_memory_duplicates(
    *,
    session: AsyncSession,
    memory_repo: MemoryRepository,
    user_id: str,
    now: datetime,
) -> tuple[int, int]:
    """Every `confirmed_duplicate` row: does Neo4j actually show `memory_id_a`
    as `DUPLICATE_OF` `memory_id_b`? If not, re-apply `confirm_duplicate` --
    idempotent, so this is safe whether the original Neo4j write never landed
    at all or landed and this is just a defensive re-check.
    """
    review_repo = MemoryReviewCandidateRepository(session, user_id=user_id)
    rows = await review_repo.list_by_status(status="confirmed_duplicate", limit=_SCAN_LIMIT)
    checked = 0
    repaired = 0
    for row in rows:
        checked += 1
        try:
            detail = await memory_repo.get(user_id=user_id, memory_id=row["memory_id_a"])
        except AppError:
            logger.warning(
                "Reconciliation: could not read memory %s for review candidate %s",
                row["memory_id_a"],
                row["id"],
            )
            continue
        if detail is None or row["memory_id_b"] not in detail.duplicate_of:
            try:
                await memory_repo.confirm_duplicate(
                    user_id=user_id,
                    duplicate_id=row["memory_id_a"],
                    canonical_id=row["memory_id_b"],
                    now=now,
                )
                repaired += 1
                logger.info("Reconciliation: repaired memory duplicate %s", row["id"])
            except AppError:
                logger.exception(
                    "Reconciliation: failed to repair memory duplicate %s", row["id"]
                )
    return checked, repaired


async def _reconcile_entity_merges(
    *,
    session: AsyncSession,
    entity_repo: EntityRepository,
    user_id: str,
    now: datetime,
    id_fn: Callable[[], str],
) -> tuple[int, int]:
    """Every `merged` row: does resolving the merged entity actually land on
    the recorded canonical entity? If not, re-apply `EntityRepository.merge`
    with the exact `merge_id`/canonical/merged ids the `MERGE_ENTITY` audit
    entry recorded -- never a freshly-chosen pair, since only a human review
    action may decide that.
    """
    merge_repo = EntityMergeCandidateRepository(session, user_id=user_id)
    audit_repo = ReviewAuditRepository(session, user_id=user_id)
    rows = await merge_repo.list_by_status(status="merged", limit=_SCAN_LIMIT)
    checked = 0
    repaired = 0
    for row in rows:
        checked += 1
        entries = await audit_repo.list_for_target(
            target_type="entity_merge_candidate", target_id=row["id"]
        )
        merge_entry = next((e for e in reversed(entries) if e["action"] == "MERGE_ENTITY"), None)
        if merge_entry is None:
            logger.warning(
                "Reconciliation: candidate %s is 'merged' with no MERGE_ENTITY audit "
                "entry -- cannot safely repair, skipping",
                row["id"],
            )
            continue
        resulting = merge_entry["resulting_state"]
        canonical_id = resulting.get("canonical_entity_id")
        merged_id = resulting.get("merged_entity_id")
        merge_id = resulting.get("merge_id")
        if not (canonical_id and merged_id and merge_id):
            continue
        try:
            resolved = await entity_repo.resolve(user_id=user_id, entity_id=merged_id)
        except AppError:
            logger.warning(
                "Reconciliation: could not resolve entity %s for candidate %s",
                merged_id,
                row["id"],
            )
            continue
        if resolved is None or resolved.entity.id != canonical_id:
            try:
                await entity_repo.merge(
                    user_id=user_id,
                    entity_id_a=canonical_id,
                    entity_id_b=merged_id,
                    merge_id=merge_id,
                    new_aliases=[],
                    excluded_statuses=[],
                    now=now,
                    id_fn=id_fn,
                )
                repaired += 1
                logger.info("Reconciliation: repaired entity merge %s", row["id"])
            except AppError:
                logger.exception("Reconciliation: failed to repair entity merge %s", row["id"])
    return checked, repaired


async def run_reconciliation_tick(
    *,
    db: Database,
    graph_client: GraphClient,
    user_id: str,
    now_fn: Callable[[], datetime] = _utcnow,
    id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> dict[str, int]:
    """One bounded, resumable pass over one user's terminal-status review rows,
    repairing any PostgreSQL-records-a-decision-Neo4j-never-applied gap by
    re-running the original (idempotent) Neo4j write. Never touches rows still
    `unreviewed` -- there is nothing to reconcile until a decision exists.
    """
    now = now_fn()
    memory_repo = MemoryRepository(graph_client)
    entity_repo = EntityRepository(graph_client)
    async with db.session() as session:
        dup_checked, dup_repaired = await _reconcile_memory_duplicates(
            session=session, memory_repo=memory_repo, user_id=user_id, now=now
        )
        merge_checked, merge_repaired = await _reconcile_entity_merges(
            session=session, entity_repo=entity_repo, user_id=user_id, now=now, id_fn=id_fn
        )
    return {
        "memory_duplicates_checked": dup_checked,
        "memory_duplicates_repaired": dup_repaired,
        "entity_merges_checked": merge_checked,
        "entity_merges_repaired": merge_repaired,
    }
