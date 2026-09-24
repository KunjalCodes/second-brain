"""The extraction job queue against real PostgreSQL (docs/PHASE2_PLAN.md section 7).

Covers what no unit test can: `SELECT ... FOR UPDATE SKIP LOCKED` concurrency
safety, the idempotency constraints as real unique indexes, and the claim/lease/
retry state machine end to end.

Every unit of work here opens its own short-lived, auto-committing session via
`db.session()` -- matching how the real application works (one session per
request/job) -- rather than holding one session open across a whole test. A shared,
uncommitted session's writes are invisible to any other session under PostgreSQL's
transaction isolation, which would make the claim/reclaim assertions below silently
see nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from app.models.conversation import MessageRole
from app.models.extraction import ExtractionJobStatus
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.extraction_queue import ExtractionQueue
from app.repositories.extraction_repository import ExtractionJobRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from tests.conftest import JAN, MAY

pytestmark = pytest.mark.integration

EXTRACTOR_VERSION = "fake:default:v1:v1"


@pytest.fixture
async def conversation_id(db, user_id: str) -> str:  # noqa: ANN001
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="t", now=JAN
        )
        return conv.id


@pytest.fixture
async def message_id(db, user_id: str, conversation_id: str) -> str:  # noqa: ANN001
    async with db.session() as session:
        message = await MessageRepository(session, user_id=user_id).append(
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content="The project uses Gemini for classification.",
            token_count=None,
            now=JAN,
        )
        return message.id


async def _create_job(
    db, *, user_id: str, conversation_id: str, message_id: str,  # noqa: ANN001
    window_start: int = 0, window_end: int = 0, extractor_version: str = EXTRACTOR_VERSION,
):
    async with db.session() as session:
        return await ExtractionJobRepository(session, user_id=user_id).create_job(
            job_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            target_message_id=message_id,
            window_start_sequence=window_start,
            window_end_sequence=window_end,
            extractor_version=extractor_version,
            now=JAN,
        )


async def _get_job(db, *, user_id: str, job_id: str):  # noqa: ANN001
    async with db.session() as session:
        return await ExtractionJobRepository(session, user_id=user_id).get(job_id=job_id)


class TestJobCreationIdempotency:
    async def test_creating_twice_with_the_same_key_returns_the_same_job(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        first = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        second = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        assert first.id == second.id

    async def test_a_different_extractor_version_creates_a_new_job(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """A deliberate prompt/model change must be able to re-extract."""
        first = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id,
            extractor_version="fake:default:v1:v1",
        )
        second = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id,
            extractor_version="fake:default:v2:v1",
        )
        assert first.id != second.id


class TestDebounce:
    async def test_extending_a_pending_job_does_not_create_a_second_job(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        job = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            extended = await ExtractionJobRepository(session, user_id=user_id).extend_job(
                job_id=job.id, target_message_id=message_id, window_end_sequence=3
            )
        assert extended.id == job.id
        assert extended.window_end_sequence == 3

    async def test_a_locked_pending_job_is_not_offered_for_debounce(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """A job a worker is actively claiming/processing must not be extended --
        that would race with the worker's read of the window.
        """
        await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w1", now=JAN, max_attempts=3
            )
        async with db.session() as session:
            pending = await ExtractionJobRepository(
                session, user_id=user_id
            ).find_pending_job_for_conversation(conversation_id=conversation_id)
        assert pending is None


class TestClaiming:
    async def test_claim_marks_the_job_in_progress_and_increments_attempts(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        job = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            claimed = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w1", now=JAN, max_attempts=3
            )
        assert [c.id for c in claimed] == [job.id]
        assert claimed[0].status is ExtractionJobStatus.IN_PROGRESS
        assert claimed[0].attempts == 1
        assert claimed[0].user_id == user_id  # ownership survives the global claim

    async def test_an_already_claimed_job_is_not_claimed_again(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as s1:
            first = await ExtractionQueue(s1).claim_batch(
                limit=5, worker_id="w1", now=JAN, max_attempts=3
            )
        async with db.session() as s2:
            second = await ExtractionQueue(s2).claim_batch(
                limit=5, worker_id="w2", now=JAN, max_attempts=3
            )
        assert len(first) == 1
        assert second == []

    async def test_concurrent_claims_never_return_the_same_job_twice(
        self, db, user_id: str, conversation_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """The core SKIP LOCKED guarantee: two workers racing to claim must
        partition the available jobs, never double-claim one.
        """
        async with db.session() as session:
            msg_repo = MessageRepository(session, user_id=user_id)
            job_repo = ExtractionJobRepository(session, user_id=user_id)
            for i in range(6):
                other_message = await msg_repo.append(
                    message_id=str(uuid.uuid4()), conversation_id=conversation_id,
                    role=MessageRole.USER, content=f"message number {i} in the batch",
                    token_count=None, now=JAN,
                )
                await job_repo.create_job(
                    job_id=str(uuid.uuid4()), conversation_id=conversation_id,
                    target_message_id=other_message.id,
                    window_start_sequence=i, window_end_sequence=i,
                    extractor_version=EXTRACTOR_VERSION, now=JAN,
                )

        async def claim_some():  # noqa: ANN202
            async with db.session() as session:
                return await ExtractionQueue(session).claim_batch(
                    limit=3, worker_id=f"w-{uuid.uuid4()}", now=JAN, max_attempts=3
                )

        results = await asyncio.gather(*(claim_some() for _ in range(4)))
        all_claimed_ids = [j.id for batch in results for j in batch]
        assert len(all_claimed_ids) == 6
        assert len(all_claimed_ids) == len(set(all_claimed_ids)), "a job was claimed twice"

    async def test_claim_respects_the_limit(
        self, db, user_id: str, conversation_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """Every job below shares the exact same `created_at` (`now=JAN`) on
        purpose: this is the regression test for a real bug found during the
        Phase 2 hardening pass (docs/PHASE2_HARDENING.md). With `ORDER BY
        created_at` alone and a `WHERE id IN (subquery)` claim shape, PostgreSQL
        was free to plan the match as a nested-loop semi join that re-executes the
        `LIMIT ... FOR UPDATE SKIP LOCKED` subquery once per row rather than once
        overall -- and did so under real full-suite conditions, claiming all 5 jobs
        for a `limit=2` call. The fix (`ExtractionQueue.claim_batch`) uses a CTE to
        force single evaluation and adds `id` as an `ORDER BY` tiebreaker; this
        test is what would have caught it, and pins the fix in place.
        """
        async with db.session() as session:
            msg_repo = MessageRepository(session, user_id=user_id)
            job_repo = ExtractionJobRepository(session, user_id=user_id)
            for i in range(5):
                msg = await msg_repo.append(
                    message_id=str(uuid.uuid4()), conversation_id=conversation_id,
                    role=MessageRole.USER, content=f"content number {i} here",
                    token_count=None, now=JAN,
                )
                await job_repo.create_job(
                    job_id=str(uuid.uuid4()), conversation_id=conversation_id,
                    target_message_id=msg.id, window_start_sequence=i, window_end_sequence=i,
                    extractor_version=EXTRACTOR_VERSION, now=JAN,
                )
        async with db.session() as session:
            claimed = await ExtractionQueue(session).claim_batch(
                limit=2, worker_id="w1", now=JAN, max_attempts=3
            )
        assert len(claimed) == 2


class TestRetryAndBackoff:
    async def test_failed_job_is_not_claimable_before_next_attempt_at(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        job = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w1", now=JAN, max_attempts=3
            )
        async with db.session() as session:
            await ExtractionQueue(session).mark_failed(
                job_id=job.id, error_code="llm_unavailable", error_detail="timeout",
                permanent=False, next_attempt_at=JAN + timedelta(hours=1), now=JAN,
            )
        async with db.session() as session:
            claimed_too_early = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w2", now=JAN, max_attempts=3
            )
        assert claimed_too_early == []

        async with db.session() as session:
            claimed_after_backoff = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w3", now=JAN + timedelta(hours=2), max_attempts=3
            )
        assert len(claimed_after_backoff) == 1
        assert claimed_after_backoff[0].attempts == 2

    async def test_permanent_failure_is_never_retried(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        job = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w1", now=JAN, max_attempts=3
            )
        async with db.session() as session:
            await ExtractionQueue(session).mark_failed(
                job_id=job.id, error_code="configuration_error", error_detail="bad api key",
                permanent=True, next_attempt_at=None, now=JAN,
            )
        async with db.session() as session:
            never_claimed = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w2", now=MAY, max_attempts=3
            )
        assert never_claimed == []
        fetched = await _get_job(db, user_id=user_id, job_id=job.id)
        assert fetched.status is ExtractionJobStatus.ABANDONED

    async def test_attempts_are_bounded_by_max_attempts(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        now = JAN
        for _ in range(3):  # max_attempts=3: claim, fail, claim, fail, claim, fail
            async with db.session() as session:
                claimed = await ExtractionQueue(session).claim_batch(
                    limit=5, worker_id="w", now=now, max_attempts=3
                )
            assert len(claimed) == 1
            async with db.session() as session:
                await ExtractionQueue(session).mark_failed(
                    job_id=claimed[0].id, error_code="llm_unavailable", error_detail="x",
                    permanent=False, next_attempt_at=now, now=now,
                )
            now = now + timedelta(hours=1)
        async with db.session() as session:
            exhausted = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w", now=now, max_attempts=3
            )
        assert exhausted == [], "a job must not be claimable past max_attempts"


class TestLeaseReclaim:
    async def test_expired_lease_is_reclaimed_and_becomes_claimable(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        job = await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="dead-worker", now=JAN, max_attempts=3
            )
        # Simulate the worker dying: no mark_done/mark_failed ever happens. The
        # sweep must eventually notice the lease has expired.
        much_later = JAN + timedelta(hours=1)
        async with db.session() as session:
            reclaimed = await ExtractionQueue(session).reclaim_expired_leases(
                lease_seconds=600, max_attempts=3, now=much_later
            )
        assert reclaimed == 1

        async with db.session() as session:
            claimed_again = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="new-worker", now=much_later, max_attempts=3
            )
        assert [c.id for c in claimed_again] == [job.id]

    async def test_lease_reclaim_past_max_attempts_abandons_instead(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """A job that keeps crashing its worker must not be reclaimed forever --
        it funnels onto the same attempts-bounded path as an ordinary failure.
        """
        await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            claimed = await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w", now=JAN, max_attempts=1
            )
        assert claimed[0].attempts == 1  # already at max_attempts=1

        much_later = JAN + timedelta(hours=1)
        async with db.session() as session:
            await ExtractionQueue(session).reclaim_expired_leases(
                lease_seconds=600, max_attempts=1, now=much_later
            )
        fetched = await _get_job(db, user_id=user_id, job_id=claimed[0].id)
        assert fetched.status is ExtractionJobStatus.ABANDONED

    async def test_reclaim_is_idempotent(
        self, db, user_id: str, conversation_id: str, message_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        await _create_job(
            db, user_id=user_id, conversation_id=conversation_id, message_id=message_id
        )
        async with db.session() as session:
            await ExtractionQueue(session).claim_batch(
                limit=5, worker_id="w", now=JAN, max_attempts=3
            )
        much_later = JAN + timedelta(hours=1)
        async with db.session() as session:
            first = await ExtractionQueue(session).reclaim_expired_leases(
                lease_seconds=600, max_attempts=3, now=much_later
            )
        async with db.session() as session:
            second = await ExtractionQueue(session).reclaim_expired_leases(
                lease_seconds=600, max_attempts=3, now=much_later
            )
        assert first == 1
        assert second == 0, "reclaiming twice must not double-count or re-touch the same job"


class TestNoDestructiveStatements:
    def test_queue_and_repository_modules_never_delete(self) -> None:
        """Jobs and candidates are append-only, matching the Neo4j memory model --
        only their status changes; nothing here may issue a DELETE.
        """
        import inspect

        import app.repositories.extraction_queue as queue_module
        import app.repositories.extraction_repository as repo_module

        for module in (queue_module, repo_module):
            source = inspect.getsource(module)
            assert "delete(" not in source.lower()
            assert "DELETE FROM" not in source.upper()
