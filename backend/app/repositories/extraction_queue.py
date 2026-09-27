"""The extraction job queue: claim, lease, complete, fail, and the sweep's reads.

**Deliberately not a `UserScopedRepository`.** A worker tick processes jobs across
every user by design -- that is the whole point of a shared queue -- so there is no
single request-scoped `user_id` to filter by here. This is why this file (like
`sql_base.py`) is exempted from the `select`/`update`/`delete` import ban in
`tests/unit/test_sql_safety.py`, and why the exemption is kept to exactly this file:
every method that legitimately needs per-user scoping instead lives in
`app/repositories/extraction_repository.py`, which does extend
`UserScopedRepository` and is covered by the audit.

Every job and message this module touches still carries its own `user_id` column,
and callers (`app/extraction/runner.py`) are required to use *that* value -- taken
from the claimed row, never from ambient request context -- for every downstream
write. `tests/integration/test_extraction_queue.py` verifies claimed jobs carry the
correct owner and that claiming never returns another job's ownership by mistake.

Retries are unified onto one path (docs/adr/0008, docs/PHASE2_PLAN.md section 7):
a fresh job starts `pending` and is claimed unconditionally once; every subsequent
attempt -- whether the previous attempt failed outright or its worker died --
becomes `failed` with a `next_attempt_at`, and is claimed again only if
`attempts < max_attempts`. This is what bounds total attempts regardless of *why*
a job needed to be retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentChunk as ChunkRow
from app.db.models import ExtractionJob as JobRow
from app.db.models import Message as MessageRow
from app.models.conversation import ExtractionStatus, Message, MessageRole
from app.models.extraction import ExtractionJob, ExtractionJobStatus
from app.repositories.extraction_repository import _job_to_domain
from app.repositories.message_repository import _to_domain as _message_to_domain


class ExtractionQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_batch(
        self,
        *,
        limit: int,
        worker_id: str,
        now: datetime,
        max_attempts: int,
    ) -> list[ExtractionJob]:
        """Atomically claim up to `limit` jobs, across all users.

        Uses the standard Postgres claim pattern: `SELECT ... FOR UPDATE SKIP
        LOCKED` picks rows no other transaction currently holds, and the enclosing
        `UPDATE` marks them `in_progress` with a lease in the same statement, so no
        other worker can claim the same rows between the read and the write.

        The claimable set is a CTE, not a `WHERE id IN (subquery)` -- this is not
        stylistic. With `IN (...)`, PostgreSQL is free to plan the match as a
        nested-loop semi join that re-executes the `LIMIT ... FOR UPDATE SKIP
        LOCKED` subquery once per candidate row on the UPDATE's target table,
        rather than evaluating it exactly once (`docs/PHASE2_HARDENING.md`
        "transaction boundaries" found this live: with several same-timestamp
        pending jobs -- the normal case, since `created_at` had no tiebreaker --
        `claim_batch(limit=2)` claimed all 5 candidates instead of 2). A CTE forces
        single evaluation, and `ORDER BY created_at, id` gives ties a deterministic
        order regardless.
        """
        claimable = (
            select(JobRow.id)
            .where(
                or_(
                    JobRow.status == ExtractionJobStatus.PENDING.value,
                    and_(
                        JobRow.status == ExtractionJobStatus.FAILED.value,
                        JobRow.attempts < max_attempts,
                        JobRow.next_attempt_at.is_not(None),
                        JobRow.next_attempt_at <= now,
                    ),
                )
            )
            .order_by(JobRow.created_at, JobRow.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("claimable")
        )
        stmt = (
            update(JobRow)
            .where(JobRow.id == claimable.c.id)
            .values(
                status=ExtractionJobStatus.IN_PROGRESS.value,
                locked_at=now,
                locked_by=worker_id,
                attempts=JobRow.attempts + 1,
            )
            .returning(JobRow)
        )
        result = await self._session.execute(stmt)
        return [_job_to_domain(row) for row in result.scalars()]

    async def reclaim_expired_leases(
        self, *, lease_seconds: int, max_attempts: int, now: datetime
    ) -> int:
        """Recover jobs whose worker died mid-run.

        A job past its lease is folded onto the same `failed` retry path a normal
        LLM failure would take -- immediately claimable if attempts remain,
        `abandoned` otherwise. This is what stops a job that always crashes its
        worker from being reclaimed as `pending` forever.
        """
        cutoff = now - timedelta(seconds=lease_seconds)

        retryable = (
            update(JobRow)
            .where(
                JobRow.status == ExtractionJobStatus.IN_PROGRESS.value,
                JobRow.locked_at < cutoff,
                JobRow.attempts < max_attempts,
            )
            .values(
                status=ExtractionJobStatus.FAILED.value,
                next_attempt_at=now,
                error_code="lease_expired",
                error_detail="Worker did not complete the job before its lease expired",
                locked_at=None,
                locked_by=None,
            )
        )
        exhausted = (
            update(JobRow)
            .where(
                JobRow.status == ExtractionJobStatus.IN_PROGRESS.value,
                JobRow.locked_at < cutoff,
                JobRow.attempts >= max_attempts,
            )
            .values(
                status=ExtractionJobStatus.ABANDONED.value,
                completed_at=now,
                error_code="lease_expired",
                error_detail=(
                    "Worker did not complete the job before its lease expired, "
                    "and no attempts remain"
                ),
            )
        )
        retried = await self._session.execute(retryable)
        given_up = await self._session.execute(exhausted)
        return (retried.rowcount or 0) + (given_up.rowcount or 0)

    def _owned(self, job_id: str, owner: str | None):  # noqa: ANN202
        """WHERE clause for a completion write (Phase 11, J4).

        With an `owner` (the lease token the claim wrote into `locked_by`), the
        write applies only while that worker still holds the lease: once a
        lease expires and `reclaim_expired_leases` clears `locked_by`, or
        another worker re-claims the job, the stale worker's write matches zero
        rows. The check is a conditional UPDATE, so it is enforced by the
        database, not by application-level reads. `owner=None` keeps the
        unconditional legacy behaviour for callers that never claimed.
        """
        clause = JobRow.id == job_id
        if owner is not None:
            clause = and_(
                clause,
                JobRow.status == ExtractionJobStatus.IN_PROGRESS.value,
                JobRow.locked_by == owner,
            )
        return clause

    async def renew_lease(self, *, job_id: str, owner: str, now: datetime) -> bool:
        """Extend the lease of a job this worker still owns. False = lost it."""
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(locked_at=now)
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def mark_done(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        candidate_count: int,
        now: datetime,
        owner: str | None = None,
    ) -> bool:
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(
                status=ExtractionJobStatus.DONE.value,
                completed_at=now,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                candidate_count=candidate_count,
                error_code=None,
                error_detail=None,
                next_attempt_at=None,
                locked_at=None,
                locked_by=None,
            )
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def mark_failed(
        self,
        *,
        job_id: str,
        error_code: str,
        error_detail: str,
        permanent: bool,
        next_attempt_at: datetime | None,
        now: datetime,
        owner: str | None = None,
    ) -> bool:
        """Record a failure. The caller decides `permanent` (docs/PHASE2_PLAN.md
        section 9): a config/auth error never retries; a transient one gets a
        `next_attempt_at` and is retried through the normal claim path, bounded by
        `max_attempts` there. Returns False when `owner` no longer holds the
        lease (the write was not applied).
        """
        status = (
            ExtractionJobStatus.ABANDONED if permanent else ExtractionJobStatus.FAILED
        )
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(
                status=status.value,
                error_code=error_code,
                error_detail=error_detail[:500],
                next_attempt_at=None if permanent else next_attempt_at,
                completed_at=now if permanent else None,
                locked_at=None,
                locked_by=None,
            )
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def defer(
        self, *, job_id: str, owner: str, until: datetime, error_code: str, error_detail: str
    ) -> bool:
        """Put an owned job back to `failed` WITHOUT consuming an attempt.

        For failures that say nothing about the job itself -- provider
        throttling (HTTP 429 / cooldown) or a busy shared resource -- so a long
        provider outage cannot exhaust `max_attempts` and abandon healthy work
        (Phase 11, docs/PHASE11_PLAN.md section 12). Bounded separately by the
        caller's max-age check.
        """
        stmt = (
            update(JobRow)
            .where(self._owned(job_id, owner))
            .values(
                status=ExtractionJobStatus.FAILED.value,
                attempts=JobRow.attempts - 1,
                error_code=error_code,
                error_detail=error_detail[:500],
                next_attempt_at=until,
                locked_at=None,
                locked_by=None,
            )
        )
        result = await self._session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def abandon_exhausted(self, *, max_attempts: int, now: datetime) -> int:
        """Make retry exhaustion explicit (Phase 11, J5).

        A `failed` job with no attempts left is never claimed again, so leaving
        it `failed` makes it indistinguishable from work that is merely waiting
        for its backoff. It becomes `abandoned`, keeping its last `error_code`
        and `error_detail`.
        """
        stmt = (
            update(JobRow)
            .where(
                JobRow.status == ExtractionJobStatus.FAILED.value,
                JobRow.attempts >= max_attempts,
            )
            .values(
                status=ExtractionJobStatus.ABANDONED.value,
                completed_at=now,
                next_attempt_at=None,
            )
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def fail_targets_of_abandoned_jobs(self) -> tuple[int, list[tuple[str, str]]]:
        """Propagate `abandoned` to the job's target (Phase 11, J5/J7).

        Idempotent. A message whose passive extraction job was abandoned
        becomes `failed` instead of `queued` forever; a document chunk becomes
        `failed`, so its document can finalize. Returns the number of messages
        updated and the `(user_id, document_id)` pairs whose chunks changed.
        """
        abandoned_messages = select(JobRow.target_message_id).where(
            JobRow.status == ExtractionJobStatus.ABANDONED.value,
            JobRow.target_message_id.is_not(None),
            JobRow.extractor_version.not_like("agent:%"),
        )
        non_terminal = (
            ExtractionStatus.PENDING.value,
            ExtractionStatus.QUEUED.value,
            ExtractionStatus.IN_PROGRESS.value,
        )
        messages = await self._session.execute(
            update(MessageRow)
            .where(
                MessageRow.id.in_(abandoned_messages),
                MessageRow.extraction_status.in_(non_terminal),
            )
            .values(extraction_status=ExtractionStatus.FAILED.value)
        )
        abandoned_chunks = select(JobRow.target_chunk_id).where(
            JobRow.status == ExtractionJobStatus.ABANDONED.value,
            JobRow.target_chunk_id.is_not(None),
        )
        chunks = await self._session.execute(
            update(ChunkRow)
            .where(
                ChunkRow.id.in_(abandoned_chunks),
                ChunkRow.extraction_status.in_(non_terminal),
            )
            .values(extraction_status=ExtractionStatus.FAILED.value)
            .returning(ChunkRow.user_id, ChunkRow.document_id)
        )
        documents = sorted({(row[0], row[1]) for row in chunks})
        return messages.rowcount or 0, documents

    async def list_skipped_for_recheck(
        self, *, lookback_days: int, limit: int, now: datetime
    ) -> list[Message]:
        """Gate-skipped messages worth another look, across all users.

        A message the live-path gate skipped is re-evaluated later under the
        *window* gate (docs/adr/0008): context that did not exist yet when the
        message first arrived may make it meaningful in retrospect. Bounded by
        `lookback_days` and `limit` so this can never become an unbounded scan.
        """
        cutoff = now - timedelta(days=lookback_days)
        stmt = (
            select(MessageRow)
            .where(
                MessageRow.role == MessageRole.USER.value,
                MessageRow.extraction_status == ExtractionStatus.SKIPPED.value,
                MessageRow.created_at >= cutoff,
            )
            .order_by(MessageRow.created_at)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_message_to_domain(row) for row in result.scalars()]
