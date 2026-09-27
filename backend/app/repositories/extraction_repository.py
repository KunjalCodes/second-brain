"""User-scoped extraction persistence: job creation/debounce and candidate writes.

Two repositories here extend `UserScopedRepository` and are covered by the SQL
isolation audit (`tests/unit/test_sql_safety.py`): job creation happens from the
live request path with a known requesting user, and candidate writes happen from
the runner using the claimed job's own `user_id` (never an ambient one).

The genuinely cross-user queue operations -- claiming a batch of jobs regardless of
whose they are, reclaiming expired leases, marking jobs done/failed, and finding
gate-skipped messages to recheck -- are NOT here. They live in
`app/repositories/extraction_queue.py`, a deliberately separate and narrowly
exempted file, because a worker tick processes jobs across every user by design.
Splitting the two keeps that exemption to the smallest possible surface.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from app.db.models import ExtractionCandidate as CandidateRow
from app.db.models import ExtractionJob as JobRow
from app.models.extraction import (
    DecisionType,
    ExtractionCandidateRecord,
    ExtractionJob,
    ExtractionJobStatus,
    ExtractionSourceKind,
)
from app.repositories.sql_base import UserScopedRepository


def candidate_hash(payload: dict[str, Any]) -> str:
    """Deterministic hash of a validated candidate, for effect-level idempotency.

    `sort_keys=True` makes the hash independent of field order, so the same
    candidate produces the same hash regardless of how the provider ordered JSON
    keys across a retry.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _job_to_domain(row: JobRow) -> ExtractionJob:
    return ExtractionJob(
        id=row.id,
        user_id=row.user_id,
        source_kind=ExtractionSourceKind(row.source_kind),
        conversation_id=row.conversation_id,
        target_message_id=row.target_message_id,
        document_id=row.document_id,
        target_chunk_id=row.target_chunk_id,
        window_start_sequence=row.window_start_sequence,
        window_end_sequence=row.window_end_sequence,
        extractor_version=row.extractor_version,
        status=row.status,
        attempts=row.attempts,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        next_attempt_at=row.next_attempt_at,
        error_code=row.error_code,
        error_detail=row.error_detail,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
        candidate_count=row.candidate_count,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def _candidate_to_domain(row: CandidateRow) -> ExtractionCandidateRecord:
    return ExtractionCandidateRecord(
        id=row.id,
        extraction_job_id=row.extraction_job_id,
        user_id=row.user_id,
        source_message_id=row.source_message_id,
        source_chunk_id=row.source_chunk_id,
        ordinal=row.ordinal,
        candidate_hash=row.candidate_hash,
        payload=row.payload,
        decision=row.decision,
        decision_reason=row.decision_reason,
        confidence=float(row.confidence) if row.confidence is not None else None,
        memory_id=row.memory_id,
        applied_at=row.applied_at,
        created_at=row.created_at,
    )


class ExtractionJobRepository(UserScopedRepository):
    """Job creation and the debounce lookup used by the live append-message path."""

    async def create_job(
        self,
        *,
        job_id: str,
        conversation_id: str,
        target_message_id: str,
        window_start_sequence: int,
        window_end_sequence: int,
        extractor_version: str,
        now: datetime,
        claimed_by: str | None = None,
    ) -> ExtractionJob:
        """Insert a job, or return the existing one for this idempotency key.

        `claimed_by` creates the job already `in_progress` under that lease
        token (Phase 11): used by the agent's synchronous memory tool, whose
        job must never sit `pending` where a background worker could claim it
        and run passive extraction on it concurrently.

        `ON CONFLICT (target_message_id, extractor_version) DO NOTHING` is the
        first idempotency layer (docs/PHASE2_PLAN.md section 7): if a job for this
        exact target and extractor version already exists, nothing new is written
        and the existing row is returned instead.
        """
        stmt = (
            insert(JobRow)
            .values(
                id=job_id,
                user_id=self._user_id,
                conversation_id=conversation_id,
                target_message_id=target_message_id,
                window_start_sequence=window_start_sequence,
                window_end_sequence=window_end_sequence,
                extractor_version=extractor_version,
                status=(
                    ExtractionJobStatus.IN_PROGRESS.value
                    if claimed_by
                    else ExtractionJobStatus.PENDING.value
                ),
                locked_by=claimed_by,
                locked_at=now if claimed_by else None,
                attempts=1 if claimed_by else 0,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["target_message_id", "extractor_version"]
            )
            .returning(JobRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _job_to_domain(row)

        # Conflict: `ON CONFLICT DO NOTHING` returns no row, so the pre-existing
        # job (with a different id -- this insert's `job_id` was never written)
        # is looked up by the unique key itself.
        stmt = self._select(JobRow).where(
            JobRow.target_message_id == target_message_id,
            JobRow.extractor_version == extractor_version,
        )
        result = await self._session.execute(stmt)
        return _job_to_domain(result.scalar_one())

    async def create_document_chunk_job(
        self,
        *,
        job_id: str,
        document_id: str,
        target_chunk_id: str,
        window_start_ordinal: int,
        window_end_ordinal: int,
        extractor_version: str,
        now: datetime,
    ) -> ExtractionJob:
        """The document-chunk counterpart to `create_job` (Phase 6, ADR 0012).

        A separate method, not a branch inside `create_job`, so the
        well-tested conversation path is touched by nothing here --
        `create_job`'s own SQL and conflict target are completely unchanged.
        Same idempotency shape: `ON CONFLICT (target_chunk_id,
        extractor_version) DO NOTHING`, the chunk-side unique constraint
        added alongside the existing message-side one (never replacing it).
        """
        stmt = (
            insert(JobRow)
            .values(
                id=job_id,
                user_id=self._user_id,
                source_kind=ExtractionSourceKind.DOCUMENT_CHUNK.value,
                document_id=document_id,
                target_chunk_id=target_chunk_id,
                window_start_sequence=window_start_ordinal,
                window_end_sequence=window_end_ordinal,
                extractor_version=extractor_version,
                status=ExtractionJobStatus.PENDING.value,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["target_chunk_id", "extractor_version"]
            )
            .returning(JobRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _job_to_domain(row)

        stmt = self._select(JobRow).where(
            JobRow.target_chunk_id == target_chunk_id,
            JobRow.extractor_version == extractor_version,
        )
        result = await self._session.execute(stmt)
        return _job_to_domain(result.scalar_one())

    async def find_pending_job_for_conversation(
        self, *, conversation_id: str
    ) -> ExtractionJob | None:
        """An unclaimed, pending job for this conversation, if one exists.

        Used to debounce a burst of messages into one extraction window rather than
        one job per message (docs/adr/0008). A job that is already claimed
        (`locked_at IS NOT NULL`) is not returned -- extending a job a worker is
        actively processing would race with that worker's read of the window.
        """
        stmt = (
            self._select(JobRow)
            .where(
                JobRow.conversation_id == conversation_id,
                JobRow.status == ExtractionJobStatus.PENDING.value,
                JobRow.locked_at.is_(None),
            )
            .order_by(JobRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _job_to_domain(row) if row is not None else None

    async def extend_job(
        self, *, job_id: str, target_message_id: str, window_end_sequence: int
    ) -> ExtractionJob:
        """Retarget a pending job to a later message instead of creating a new job.

        The row's primary key is unchanged, so the unique
        `(target_message_id, extractor_version)` constraint is not re-checked
        against the *old* target -- extending is a mutation of one row, not an
        insert.
        """
        stmt = (
            self._update(JobRow)
            .where(JobRow.id == job_id)
            .values(
                target_message_id=target_message_id,
                window_end_sequence=window_end_sequence,
            )
            .returning(JobRow)
        )
        result = await self._session.execute(stmt)
        return _job_to_domain(result.scalar_one())

    async def get(self, *, job_id: str) -> ExtractionJob | None:
        row = await self._session.get(JobRow, job_id)
        if row is None or row.user_id != self._user_id:
            return None
        return _job_to_domain(row)

    async def list_recent(self, *, limit: int, offset: int) -> list[ExtractionJob]:
        stmt = (
            self._select(JobRow)
            # `id` is a tiebreaker: two jobs created within the same timestamp
            # resolution (entirely possible under load, or with a frozen clock in
            # tests) would otherwise sort in an undefined order.
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_job_to_domain(row) for row in result.scalars()]


class ExtractionCandidateRepository(UserScopedRepository):
    """Candidate writes -- the audit boundary between LLM proposal and decision."""

    async def record_candidate(
        self,
        *,
        candidate_id: str,
        extraction_job_id: str,
        source_message_id: str | None = None,
        source_chunk_id: str | None = None,
        ordinal: int,
        payload: dict[str, Any],
        decision: DecisionType,
        decision_reason: str,
        confidence: float | None,
        now: datetime,
    ) -> tuple[ExtractionCandidateRecord, bool]:
        """Insert a candidate row, or return the existing one for this hash.

        Exactly one of `source_message_id`/`source_chunk_id` must be given
        (Phase 6, ADR 0012) -- enforced by the database CHECK constraint;
        every call site before Phase 6 passes `source_message_id` only,
        unchanged.

        Returns `(record, created)`. `created=False` means this exact candidate was
        already recorded for this job -- the second idempotency layer
        (docs/PHASE2_PLAN.md section 7): a retried job applies only what it has not
        already applied.
        """
        chash = candidate_hash(payload)
        stmt = (
            insert(CandidateRow)
            .values(
                id=candidate_id,
                extraction_job_id=extraction_job_id,
                user_id=self._user_id,
                source_message_id=source_message_id,
                source_chunk_id=source_chunk_id,
                ordinal=ordinal,
                candidate_hash=chash,
                payload=payload,
                decision=decision.value,
                decision_reason=decision_reason,
                confidence=confidence,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["extraction_job_id", "candidate_hash"]
            )
            .returning(CandidateRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _candidate_to_domain(row), True

        stmt = self._select(CandidateRow).where(
            CandidateRow.extraction_job_id == extraction_job_id,
            CandidateRow.candidate_hash == chash,
        )
        result = await self._session.execute(stmt)
        return _candidate_to_domain(result.scalar_one()), False

    async def mark_applied(
        self, *, candidate_id: str, memory_id: str | None, now: datetime
    ) -> None:
        stmt = (
            self._update(CandidateRow)
            .where(CandidateRow.id == candidate_id)
            .values(memory_id=memory_id, applied_at=now)
        )
        await self._session.execute(stmt)

    async def applied_hashes_for_job(self, *, extraction_job_id: str) -> set[str]:
        """Hashes of candidates already applied for this job.

        Used by the runner to skip re-applying work a previous, interrupted attempt
        already completed.
        """
        stmt = self._select(CandidateRow).where(
            CandidateRow.extraction_job_id == extraction_job_id,
            CandidateRow.applied_at.is_not(None),
        )
        result = await self._session.execute(stmt)
        return {row.candidate_hash for row in result.scalars()}

    async def list_for_job(
        self, *, extraction_job_id: str
    ) -> list[ExtractionCandidateRecord]:
        stmt = (
            self._select(CandidateRow)
            .where(CandidateRow.extraction_job_id == extraction_job_id)
            .order_by(CandidateRow.ordinal)
        )
        result = await self._session.execute(stmt)
        return [_candidate_to_domain(row) for row in result.scalars()]
