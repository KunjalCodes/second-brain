"""The reconciliation sweep (ADR 0010, PHASE4_PLAN.md sections 11/15), against
real Neo4j and real PostgreSQL.

Directly injects the documented crash window -- a PostgreSQL review row
recording a decision (`confirmed_duplicate` / `merged`) that the corresponding
Neo4j write never actually applied -- rather than trying to race the two
databases to produce it naturally, then proves `run_reconciliation_tick`
detects and repairs it by re-applying the same idempotent Neo4j write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import NotFoundError
from app.models.enums import MemoryStatus
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.review_audit_repository import ReviewAuditRepository
from app.repositories.user_repository import UserRepository
from app.services.review_reconciliation_service import run_reconciliation_tick

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _create_memory(graph, user_id: str, memory_id: str, status: MemoryStatus) -> None:  # noqa: ANN001
    await graph.write(
        """
        CREATE (m:Memory {
            id: $id, user_id: $user_id, content: $content, memory_type: 'semantic',
            status: $status, confidence: 0.9, valid_from: null, valid_until: null,
            created_at: $now, updated_at: $now, invalidation_reason: null
        })
        """,
        {"id": memory_id, "user_id": user_id, "content": f"content-{memory_id}",
         "status": status.value, "now": NOW},
    )


async def _create_entity(graph, user_id: str, entity_id: str, name: str) -> None:  # noqa: ANN001
    await graph.write(
        """
        CREATE (e:Entity {
            id: $id, user_id: $user_id, name: $name, normalized_name: $normalized_name,
            entity_type: 'other', aliases: [], created_at: $now, updated_at: $now
        })
        """,
        {"id": entity_id, "user_id": user_id, "name": name,
         "normalized_name": name.lower(), "now": NOW},
    )


class TestMemoryDuplicateReconciliation:
    async def test_detects_and_repairs_a_recorded_but_unapplied_duplicate(
        self, db, graph, user_id, pg_cleanup, cleanup  # noqa: ANN001
    ) -> None:
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await _create_memory(graph, user_id, a, MemoryStatus.ACTIVE)
        await _create_memory(graph, user_id, b, MemoryStatus.ACTIVE)

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=NOW)
            review_repo = MemoryReviewCandidateRepository(session, user_id=user_id)
            # Simulate the crash: PostgreSQL already recorded the decision,
            # but the Neo4j write (confirm_duplicate) never happened.
            await review_repo.record_candidate(
                candidate_id=str(uuid.uuid4()), kind="likely_duplicate",
                memory_id_a=a, memory_id_b=b, source_extraction_candidate_id=None,
                evidence={"reason": "test"}, score=0.95, now=NOW,
            )
            rows = await review_repo.list_unreviewed(limit=10, offset=0)
            candidate_id = rows[0]["id"]
            updated = await review_repo.try_mark_reviewed(
                candidate_id=candidate_id, if_version=1, new_status="confirmed_duplicate", now=NOW
            )
            assert updated is not None

        memory_repo = MemoryRepository(graph)
        detail_before = await memory_repo.get(user_id=user_id, memory_id=a)
        assert detail_before.memory.status is MemoryStatus.ACTIVE, "Neo4j write never happened"

        report = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        assert report["memory_duplicates_checked"] == 1
        assert report["memory_duplicates_repaired"] == 1

        detail_after = await memory_repo.get(user_id=user_id, memory_id=a)
        assert detail_after.memory.status is MemoryStatus.MERGED
        assert detail_after.duplicate_of == [b]

        # Running it again finds nothing left to repair.
        report2 = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        assert report2["memory_duplicates_checked"] == 1
        assert report2["memory_duplicates_repaired"] == 0


class TestEntityMergeReconciliation:
    async def test_detects_and_repairs_a_recorded_but_unapplied_merge(
        self, db, graph, user_id, pg_cleanup, cleanup  # noqa: ANN001
    ) -> None:
        canonical_id, merged_id = str(uuid.uuid4()), str(uuid.uuid4())
        await _create_entity(graph, user_id, canonical_id, "Canonical Co")
        await _create_entity(graph, user_id, merged_id, "Canonical Company")
        merge_id = str(uuid.uuid4())

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=NOW)
            merge_repo = EntityMergeCandidateRepository(session, user_id=user_id)
            await merge_repo.record_candidate(
                candidate_id=str(uuid.uuid4()), entity_id_a=canonical_id, entity_id_b=merged_id,
                evidence={"signal": "test"}, score=0.9, now=NOW,
            )
            rows = await merge_repo.list_unreviewed(limit=10, offset=0)
            candidate_id = rows[0]["id"]
            # Sorted ids: figure out which side the repo actually stored as a/b.
            stored_a, stored_b = rows[0]["entity_id_a"], rows[0]["entity_id_b"]
            actual_canonical = canonical_id if canonical_id in (stored_a, stored_b) else stored_a
            actual_merged = merged_id if merged_id != actual_canonical else stored_b

            updated = await merge_repo.try_mark_reviewed(
                candidate_id=candidate_id, if_version=1, new_status="merged",
                expected_current_status="unreviewed",
            )
            assert updated is not None
            # Simulate what approve_entity_merge's audit write records --
            # without ever having called EntityRepository.merge() itself.
            audit_repo = ReviewAuditRepository(session, user_id=user_id)
            await audit_repo.record(
                entry_id=str(uuid.uuid4()), action="MERGE_ENTITY",
                target_type="entity_merge_candidate", target_id=candidate_id,
                previous_state={"status": "unreviewed", "version": 1},
                resulting_state={
                    "status": "merged", "version": 2, "merge_id": merge_id,
                    "canonical_entity_id": actual_canonical, "merged_entity_id": actual_merged,
                },
                reason=None, reviewer_id=user_id, now=NOW,
            )

        entity_repo = EntityRepository(graph)
        resolved_before = await entity_repo.resolve(user_id=user_id, entity_id=actual_merged)
        assert resolved_before.was_redirected is False, "Neo4j merge never happened"

        report = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        assert report["entity_merges_checked"] == 1
        assert report["entity_merges_repaired"] == 1

        resolved_after = await entity_repo.resolve(user_id=user_id, entity_id=actual_merged)
        assert resolved_after.was_redirected is True
        assert resolved_after.entity.id == actual_canonical

        report2 = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        assert report2["entity_merges_checked"] == 1
        assert report2["entity_merges_repaired"] == 0

    async def test_leaves_a_row_with_no_merge_audit_entry_alone(
        self, db, graph, user_id, pg_cleanup, cleanup  # noqa: ANN001
    ) -> None:
        """A `merged` row with no `MERGE_ENTITY` audit entry (should be
        unreachable via the real write path) must be skipped, never guessed at.
        """
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await _create_entity(graph, user_id, a, "A")
        await _create_entity(graph, user_id, b, "B")

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=NOW)
            merge_repo = EntityMergeCandidateRepository(session, user_id=user_id)
            await merge_repo.record_candidate(
                candidate_id=str(uuid.uuid4()), entity_id_a=a, entity_id_b=b,
                evidence={"signal": "test"}, score=0.9, now=NOW,
            )
            rows = await merge_repo.list_unreviewed(limit=10, offset=0)
            candidate_id = rows[0]["id"]
            await merge_repo.try_mark_reviewed(
                candidate_id=candidate_id, if_version=1, new_status="merged",
                expected_current_status="unreviewed",
            )

        report = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        assert report["entity_merges_checked"] == 1
        assert report["entity_merges_repaired"] == 0


class TestReconciliationIsUserScoped:
    async def test_reconciliation_never_touches_another_users_rows(
        self, db, graph, user_id, other_user_id, pg_cleanup, cleanup  # noqa: ANN001
    ) -> None:
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await _create_memory(graph, other_user_id, a, MemoryStatus.ACTIVE)
        await _create_memory(graph, other_user_id, b, MemoryStatus.ACTIVE)

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=NOW)
            await UserRepository(session).ensure_exists(other_user_id, now=NOW)
            review_repo = MemoryReviewCandidateRepository(session, user_id=other_user_id)
            await review_repo.record_candidate(
                candidate_id=str(uuid.uuid4()), kind="likely_duplicate",
                memory_id_a=a, memory_id_b=b, source_extraction_candidate_id=None,
                evidence={"reason": "test"}, score=0.95, now=NOW,
            )
            rows = await review_repo.list_unreviewed(limit=10, offset=0)
            await review_repo.try_mark_reviewed(
                candidate_id=rows[0]["id"], if_version=1, new_status="confirmed_duplicate", now=NOW
            )

        # Reconciling `user_id` (not `other_user_id`) must find nothing.
        report = await run_reconciliation_tick(db=db, graph_client=graph, user_id=user_id)
        assert report["memory_duplicates_checked"] == 0

        memory_repo = MemoryRepository(graph)
        other_detail = await memory_repo.get(user_id=other_user_id, memory_id=a)
        assert other_detail.memory.status is MemoryStatus.ACTIVE, "must be untouched"

        with pytest.raises(NotFoundError):
            # sanity: confirming across users is impossible through the real
            # write path either (the Cypher's own $user_id filter finds
            # neither memory) -- included so this test documents both halves
            # of the isolation guarantee in one place.
            await memory_repo.confirm_duplicate(
                user_id=user_id, duplicate_id=a, canonical_id=b, now=NOW
            )
