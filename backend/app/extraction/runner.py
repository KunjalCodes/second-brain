"""The extraction pipeline's orchestration layer (docs/PHASE2_PLAN.md section 4).

This is where every deterministic piece -- grounding, resolution, the decision
engine -- meets I/O: the LLM call, Neo4j (via the unmodified Phase 1
`MemoryService`/`RetrievalService`), and PostgreSQL (candidate bookkeeping,
job/queue state).

## The pipeline, per job

    load window messages
      -> build prompt -> LLMClient.complete_structured (untrusted JSON in)
      -> Pydantic validation (already done by complete_structured)
      -> per candidate:
           persistence gate -> IGNORE?
           grounding check   -> INVALID?
           resolve entities  -> flag ambiguous pairs (never merge)
           classify relationship(s) against existing Neo4j state
           persist via the Phase 1 MemoryService (NEW/UPDATE/SUPERSEDING/
             CONFLICTING/DUPLICATE) -- or nothing (IGNORE/INVALID)
           record the candidate (audit row, idempotency layer 2)
      -> mark the job done, or failed/abandoned with a reason

A single candidate's Pydantic or domain-validation failure never aborts the whole
job: it is recorded as `INVALID` and the remaining candidates still get a chance.

Each candidate's Postgres bookkeeping commits in its own short session right after
that candidate's Neo4j write (docs/PHASE2_HARDENING.md), and an unexpected error
anywhere in the per-candidate loop marks the job failed/retryable rather than
propagating out of `run()` -- see `ExtractionRunner.run`'s docstring.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConfigurationError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
    NotFoundError,
    is_infrastructure_error,
)
from app.core.logging import bind_job, reset_job
from app.db.engine import Database
from app.embeddings.base import EmbeddingClient
from app.extraction.decisions import (
    ExistingEdge,
    RelationshipDecision,
    classify_relationship,
    overall_decision,
)
from app.extraction.grounding import check_grounding
from app.extraction.resolution import EXACT_IDENTITY_TYPES, is_plausible_duplicate
from app.extraction.temporal_hints import TemporalInterpretation, interpret_temporal
from app.extraction.trigger import maybe_trigger_extraction
from app.extraction.worthiness import evaluate_worthiness
from app.graph.connection import GraphClient
from app.llm.base import LLMClient
from app.llm.prompts.document_extraction_v1 import build_document_extraction_prompt
from app.llm.prompts.extraction_v1 import build_extraction_prompt
from app.models.conversation import ExtractionStatus, Message
from app.models.document import DocumentChunk, DocumentStatus, format_chunk_source_location
from app.models.domain import Entity
from app.models.enums import Direction, MemoryStatus, SourceType
from app.models.extraction import (
    CandidateMemory,
    CandidateRelationship,
    DecisionType,
    ExtractionJob,
    ExtractionResult,
    ExtractionSourceKind,
)
from app.models.normalization import normalize_name
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.embedding_repository import EmbeddingRepository
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.extraction_queue import ExtractionQueue
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
    candidate_hash,
)
from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.message_repository import MessageRepository
from app.schemas.memory import (
    EntityInput,
    MemoryCreate,
    MemoryUpdate,
    RelationshipInput,
    SourceInput,
)
from app.semantic.embedding_pipeline import embed_and_store_memory
from app.semantic.similarity import SemanticEvidence, best_semantic_decision, cosine_similarity
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)


def _statement_key(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".!")

#: Applied per failed attempt: attempt 1 -> 60s, attempt 2 -> 120s, attempt 3 -> 240s
#: ... capped at one hour, so a prolonged provider outage does not busy-loop the
#: sweep.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600

#: Upper bound on how many of a user's existing entities are scanned for plausible
#: near-duplicates per candidate entity that doesn't already exist under its exact
#: normalized name. Keeps ambiguity detection O(1)-ish per candidate rather than
#: unbounded as a user's graph grows; a user with more entities than this will have
#: some ambiguous pairs missed rather than none, which is the acceptable direction
#: of error (a missed flag is an inconvenience, not a wrong merge).
_AMBIGUITY_SCAN_LIMIT = 500


@dataclass(frozen=True)
class CandidateApplication:
    """What `ExtractionRunner._apply_candidate` did with one candidate.

    `target_memory_id`/`evidence_score` are carried through from the governing
    `RelationshipDecision` purely so `run()` can write a `MemoryReviewCandidate`
    row (PHASE4_PLAN.md section 6) once `memory_id`'s own extraction-candidate
    row is committed -- `_apply_candidate` itself never touches the review
    table, since the FK it would need (`source_extraction_candidate_id`) is not
    deferrable and does not exist yet at this point in the transaction.
    """

    decision: DecisionType
    reason: str
    memory_id: str | None
    target_memory_id: str | None = None
    evidence_score: float | None = None


def _retry_backoff(attempts: int) -> timedelta:
    seconds = min(_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), _BACKOFF_CAP_SECONDS)
    return timedelta(seconds=seconds)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def finalize_document(
    *, db: Database, user_id: str, document_id: str, now: datetime
) -> None:
    """Roll a document's coarse status up once every one of its chunks has
    reached a terminal extraction_status (DONE or FAILED) -- the document-only
    analogue of a step conversation jobs never need (PHASE6_PLAN.md section 3's
    `DocumentStatus` lifecycle). A no-op, safe to call repeatedly, while any
    chunk is still pending/queued/in progress.

    COMPLETE when every chunk is DONE; PARTIALLY_COMPLETE when some are DONE
    and some FAILED; FAILED (`extraction_failed`) when none succeeded -- so a
    document whose extraction entirely failed is never shown as partly done.
    """
    async with db.session() as session:
        counts = await DocumentChunkRepository(
            session, user_id=user_id
        ).count_by_extraction_status(document_id=document_id)
    non_terminal = sum(
        counts.get(status.value, 0)
        for status in (
            ExtractionStatus.PENDING,
            ExtractionStatus.QUEUED,
            ExtractionStatus.IN_PROGRESS,
        )
    )
    if non_terminal > 0 or not counts:
        return
    failed = counts.get(ExtractionStatus.FAILED.value, 0)
    done = counts.get(ExtractionStatus.DONE.value, 0)
    async with db.session() as session:
        repo = DocumentRepository(session, user_id=user_id)
        if failed and not done:
            await repo.set_status(
                document_id=document_id,
                status=DocumentStatus.FAILED,
                error_code="extraction_failed",
                error_detail="No chunk of this document could be extracted",
                processed_at=now,
            )
        else:
            await repo.set_status(
                document_id=document_id,
                status=DocumentStatus.PARTIALLY_COMPLETE if failed else DocumentStatus.COMPLETE,
                processed_at=now,
            )


def _schema_problems(exc: LLMResponseInvalidError, limit: int = 8) -> str:
    """Which fields failed validation and why -- field paths and Pydantic's
    messages only, never the offending values (`input`), so it is safe to log
    and useful in the repair prompt. Providers put `exc.errors()` in
    `details["errors"]`; anything else yields ""."""
    errors = exc.details.get("errors") if isinstance(exc.details, dict) else None
    if not isinstance(errors, list):
        return ""
    parts = []
    for err in errors[:limit]:
        if isinstance(err, dict):
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('msg', '')}")
    more = len(errors) - limit
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


class ExtractionRunner:
    """Processes exactly one claimed job per `run` call.

    Constructed with a `GraphClient` and `LLMClient` (both cheap, process-wide) --
    the `Database` is passed per call, and `run` opens its own short session for
    each phase of the job (docs/PHASE2_PLAN.md section 8; docs/PHASE2_HARDENING.md):
    claiming commits in its own session, and each candidate's bookkeeping commits
    in its own session too, so a crash mid-job cannot roll back the claim or any
    already-processed candidate that already made those parts retryable/durable.
    """

    def __init__(
        self,
        *,
        graph_client: GraphClient,
        llm_client: LLMClient,
        embedding_client: EmbeddingClient,
        settings: Settings,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._graph = graph_client
        self._llm = llm_client
        self._embeddings = embedding_client
        self._settings = settings
        self._now = now_fn
        self._new_id = id_fn

    async def run(
        self,
        job: ExtractionJob,
        *,
        db: Database,
        _after_candidate: Callable[[int], None] | None = None,
    ) -> None:
        """Process one job with `job_id` bound into every log line (Phase 11, M5)."""
        tokens = bind_job(job.id, "extraction")
        try:
            await self._run(job, db=db, _after_candidate=_after_candidate)
        finally:
            reset_job(tokens)

    async def _run(
        self,
        job: ExtractionJob,
        *,
        db: Database,
        _after_candidate: Callable[[int], None] | None = None,
    ) -> None:
        """Process one already-claimed job to completion (done/failed/abandoned).

        Never raises: every failure path records itself on the job and returns, so
        one bad job cannot crash a worker tick processing several. (If even the
        failure bookkeeping cannot reach PostgreSQL, the error is logged and the
        job stays `in_progress`; its lease expires and the sweep reclaims it.)

        Each candidate's PostgreSQL bookkeeping (`record_candidate`/`mark_applied`,
        plus any ambiguity flags) is committed in its own short session immediately
        after that candidate's Neo4j write, rather than batched into one whole-job
        transaction (docs/PHASE2_HARDENING.md, "transaction boundaries"). This
        bounds the damage of a crash mid-job: every candidate processed before the
        crash is durably recorded and will not be reprocessed, so at most the
        *one* candidate that was in flight when the crash happened can possibly be
        duplicated on retry -- never repeatedly, and never any candidate before it.

        Lease ownership (Phase 11, J4): `job.locked_by` is this claim's lease
        token. The lease is renewed before the LLM call's result is applied and
        before every candidate; every completion write is conditional on still
        owning it. A worker that lost its lease stops before writing anything
        further and can never overwrite the new owner's result.

        Infrastructure failures (Phase 11, J1) -- Neo4j or PostgreSQL
        unavailable -- make the job retryable; they are never recorded as a
        semantic verdict on a candidate.

        `_after_candidate` is a testing seam only (mirrors `now_fn`/`id_fn`): a hook
        invoked after each candidate's commit, used to deterministically simulate a
        crash at an exact point in the loop.
        """
        now = self._now()
        owner = job.locked_by
        memory_service = MemoryService(
            MemoryRepository(self._graph), now_fn=self._now, id_fn=self._new_id
        )
        retrieval_service = RetrievalService(
            EntityRepository(self._graph),
            max_hops=self._settings.graph_max_hops,
            now_fn=self._now,
        )

        is_document_job = job.source_kind is ExtractionSourceKind.DOCUMENT_CHUNK

        try:
            if not await self._renew(job, db=db):
                return
            if is_document_job:
                async with db.session() as session:
                    window = await DocumentChunkRepository(
                        session, user_id=job.user_id
                    ).list_by_ordinal_range(
                        document_id=job.document_id,
                        start_ordinal=job.window_start_sequence,
                        end_ordinal=job.window_end_sequence,
                    )
                target = next((c for c in window if c.id == job.target_chunk_id), None)
                if target is None:
                    await self._fail(
                        job,
                        db=db,
                        now=now,
                        error_code="target_chunk_missing",
                        error_detail="the job's target chunk was not found in its own window",
                        permanent=True,
                    )
                    return
                source_type = SourceType.DOCUMENT
                source_reference = job.document_id
                # `reference` alone (the document id) cannot pinpoint which
                # chunk a memory came from -- unlike a message id, which is
                # already unique. Threaded through `_persist` into `Source.
                # location` alongside the usual char-span, so document
                # provenance can resolve back to an exact chunk (and, via it,
                # a page/heading) rather than only "somewhere in this
                # document".
                source_location_prefix = format_chunk_source_location(job.target_chunk_id)
                result = await self._extract_document(
                    window=window, target_chunk_id=job.target_chunk_id
                )
            else:
                async with db.session() as session:
                    window = await MessageRepository(
                        session, user_id=job.user_id
                    ).list_by_sequence_range(
                        conversation_id=job.conversation_id,
                        start_sequence=job.window_start_sequence,
                        end_sequence=job.window_end_sequence,
                    )
                target = next((m for m in window if m.id == job.target_message_id), None)
                if target is None:
                    await self._fail(
                        job,
                        db=db,
                        now=now,
                        error_code="target_message_missing",
                        error_detail="the job's target message was not found in its own window",
                        permanent=True,
                    )
                    return
                source_type = SourceType.MESSAGE
                source_reference = target.id
                source_location_prefix = None
                result = await self._extract(window=window, target_message_id=job.target_message_id)
        except LLMRateLimitedError as exc:
            await self._defer_for_provider(job, db=db, now=now, exc=exc)
            return
        except LLMUnavailableError as exc:
            await self._fail(
                job, db=db, now=now, error_code="llm_unavailable", error_detail=exc.message
            )
            return
        except LLMResponseInvalidError as exc:
            # The one repair attempt already happened inside `_extract`; a second
            # failure here is not retried -- resending the same prompt would most
            # likely reproduce the same malformed output (docs/adr/0007).
            await self._fail(
                job,
                db=db,
                now=now,
                error_code="llm_response_invalid",
                error_detail=exc.message,
                permanent=True,
            )
            return
        except ConfigurationError as exc:
            await self._fail(
                job,
                db=db,
                now=now,
                error_code="configuration_error",
                error_detail=exc.message,
                permanent=True,
            )
            return
        except Exception as exc:  # noqa: BLE001 - never let an unexpected error strand a job in_progress
            await self._fail_unexpected(job, db=db, now=now, exc=exc, stage="extracting")
            return

        # Every message's content in the window, for the entity-grounding check's
        # window-wide fallback (docs/adr/0008: pronoun resolution needs context
        # from earlier in the window, not just the target message).
        window_text = "\n\n".join(m.content for m in window)

        try:
            async with db.session() as session:
                already_applied = await ExtractionCandidateRepository(
                    session, user_id=job.user_id
                ).applied_hashes_for_job(extraction_job_id=job.id)

            for ordinal, candidate in enumerate(result.value.candidates):
                payload = candidate.model_dump(mode="json")
                chash = candidate_hash(payload)
                if chash in already_applied:
                    # Idempotency layer 2: a previous, interrupted attempt already
                    # applied this exact candidate -- do not apply it twice.
                    continue
                if not await self._renew(job, db=db):
                    return

                async with db.session() as session:
                    candidates_repo = ExtractionCandidateRepository(session, user_id=job.user_id)
                    merge_repo = EntityMergeCandidateRepository(session, user_id=job.user_id)
                    review_repo = MemoryReviewCandidateRepository(session, user_id=job.user_id)
                    embedding_repo = EmbeddingRepository(session, user_id=job.user_id)

                    applied = await self._apply_candidate(
                        candidate,
                        target_content=target.content,
                        source_type=source_type,
                        source_reference=source_reference,
                        source_location_prefix=source_location_prefix,
                        window_text=window_text,
                        user_id=job.user_id,
                        memory_service=memory_service,
                        retrieval_service=retrieval_service,
                        merge_repo=merge_repo,
                        embedding_repo=embedding_repo,
                        db=db,
                        now=now,
                        stated_at=target.created_at,
                    )
                    record, created = await candidates_repo.record_candidate(
                        candidate_id=self._new_id(),
                        extraction_job_id=job.id,
                        source_message_id=None if is_document_job else job.target_message_id,
                        source_chunk_id=job.target_chunk_id if is_document_job else None,
                        ordinal=ordinal,
                        payload=payload,
                        decision=applied.decision,
                        decision_reason=applied.reason,
                        confidence=candidate.confidence,
                        now=now,
                    )
                    if created and applied.memory_id is not None:
                        await candidates_repo.mark_applied(
                            candidate_id=record.id, memory_id=applied.memory_id, now=now
                        )
                    # The review-candidate row's FK to extraction_candidates.id is
                    # NOT DEFERRABLE, so it can only be written once that row is
                    # known to exist -- i.e. after record_candidate, in this same
                    # transaction (PHASE4_PLAN.md section 11a: "LLM proposed" ->
                    # "engine decided" is this whole atomic write).
                    if (
                        applied.memory_id is not None
                        and applied.target_memory_id is not None
                        and applied.decision
                        in (
                            DecisionType.LIKELY_DUPLICATE,
                            DecisionType.FLAGGED,
                            DecisionType.CONFLICTING,
                        )
                    ):
                        await review_repo.record_candidate(
                            candidate_id=self._new_id(),
                            kind=applied.decision.value.lower(),
                            memory_id_a=applied.memory_id,
                            memory_id_b=applied.target_memory_id,
                            source_extraction_candidate_id=record.id,
                            evidence={
                                "reason": applied.reason,
                                "similarity": applied.evidence_score,
                            },
                            score=applied.evidence_score,
                            now=now,
                        )
                # This candidate's bookkeeping just committed -- it is durable and
                # will never be reprocessed, regardless of what happens next.
                if _after_candidate is not None:
                    _after_candidate(ordinal)
        except AppError as exc:
            if is_infrastructure_error(exc):
                # Retryable: the candidates not yet applied are retried under
                # idempotency layer 2 once the dependency is back.
                logger.warning(
                    "extraction_job_infrastructure_failure job_id=%s code=%s",
                    job.id,
                    exc.code,
                )
                await self._fail(
                    job, db=db, now=now, error_code=exc.code, error_detail=exc.message
                )
            else:
                await self._fail_unexpected(job, db=db, now=now, exc=exc, stage="applying")
            return
        except Exception as exc:  # noqa: BLE001 - see the docstring's "never raises" contract
            await self._fail_unexpected(job, db=db, now=now, exc=exc, stage="applying")
            return

        try:
            async with db.session() as session:
                completed = await ExtractionQueue(session).mark_done(
                    job_id=job.id,
                    provider=result.provider,
                    model=result.model,
                    prompt_version=result.prompt_version,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                    candidate_count=len(result.value.candidates),
                    now=now,
                    owner=owner,
                )
                if not completed:
                    logger.warning(
                        "extraction_job_lease_lost job_id=%s owner=%s stage=complete",
                        job.id,
                        owner,
                    )
                    return
                if is_document_job:
                    await DocumentChunkRepository(
                        session, user_id=job.user_id
                    ).set_extraction_status(
                        chunk_id=job.target_chunk_id, status=ExtractionStatus.DONE
                    )
                else:
                    await MessageRepository(session, user_id=job.user_id).set_extraction_status(
                        message_id=job.target_message_id, status=ExtractionStatus.DONE
                    )
        except Exception:  # noqa: BLE001 - lease expiry recovers the job
            logger.exception("extraction_job_completion_write_failed job_id=%s", job.id)
            return

        if is_document_job:
            await finalize_document(
                db=db, user_id=job.user_id, document_id=job.document_id, now=now
            )

    async def _renew(self, job: ExtractionJob, *, db: Database) -> bool:
        """Renew this claim's lease; False (and a log line) if it was lost."""
        if job.locked_by is None:
            return True
        async with db.session() as session:
            owned = await ExtractionQueue(session).renew_lease(
                job_id=job.id, owner=job.locked_by, now=self._now()
            )
        if not owned:
            logger.warning(
                "extraction_job_lease_lost job_id=%s owner=%s", job.id, job.locked_by
            )
        return owned

    async def _fail(
        self,
        job: ExtractionJob,
        *,
        db: Database,
        now: datetime,
        error_code: str,
        error_detail: str,
        permanent: bool = False,
    ) -> None:
        """Record a failure on an owned job; never raises.

        A retryable failure on the job's last allowed attempt becomes terminal
        (`abandoned`) immediately, so retry exhaustion is always explicit
        (Phase 11, J5). A terminal failure also marks the job's target
        (message or chunk) `failed` and lets the document finalize.
        """
        exhausted = not permanent and job.attempts >= self._settings.extraction_max_attempts
        if exhausted:
            error_detail = f"retries exhausted after {job.attempts} attempts: {error_detail}"
        terminal = permanent or exhausted
        try:
            async with db.session() as session:
                applied = await ExtractionQueue(session).mark_failed(
                    job_id=job.id,
                    error_code=error_code,
                    error_detail=error_detail,
                    permanent=terminal,
                    next_attempt_at=None if terminal else now + _retry_backoff(job.attempts),
                    now=now,
                    owner=job.locked_by,
                )
                if not applied:
                    logger.warning(
                        "extraction_job_lease_lost job_id=%s owner=%s stage=fail",
                        job.id,
                        job.locked_by,
                    )
                    return
                if terminal:
                    await self._mark_target_failed(job, session=session)
        except Exception:  # noqa: BLE001 - lease expiry recovers the job
            logger.exception("extraction_job_failure_write_failed job_id=%s", job.id)
            return
        if terminal and job.source_kind is ExtractionSourceKind.DOCUMENT_CHUNK:
            await finalize_document(
                db=db, user_id=job.user_id, document_id=job.document_id, now=now
            )

    async def _fail_unexpected(
        self, job: ExtractionJob, *, db: Database, now: datetime, exc: Exception, stage: str
    ) -> None:
        # The traceback goes to the log; the job row (readable by the user via
        # GET /extraction/jobs/{id}) gets a safe, generic description only.
        logger.exception("extraction_job_unexpected_error job_id=%s stage=%s", job.id, stage)
        await self._fail(
            job,
            db=db,
            now=now,
            error_code="internal_error",
            error_detail=f"unexpected {type(exc).__name__} while {stage}",
        )

    async def _defer_for_provider(
        self, job: ExtractionJob, *, db: Database, now: datetime, exc: LLMRateLimitedError
    ) -> None:
        """Provider throttling (HTTP 429 / cooldown) does not consume an attempt,
        up to `llm_throttle_max_age_seconds` after the job was created; past that
        it is a normal (attempt-consuming) `llm_unavailable` failure, so the job
        still reaches a terminal state (Phase 11, docs/PHASE11_PLAN.md section 12).
        """
        age = (now - job.created_at).total_seconds()
        if age > self._settings.llm_throttle_max_age_seconds or job.locked_by is None:
            await self._fail(
                job, db=db, now=now, error_code="llm_unavailable", error_detail=exc.message
            )
            return
        wait = max(exc.retry_after_seconds or 0, _BACKOFF_BASE_SECONDS)
        try:
            async with db.session() as session:
                deferred = await ExtractionQueue(session).defer(
                    job_id=job.id,
                    owner=job.locked_by,
                    until=now + timedelta(seconds=wait),
                    error_code="llm_rate_limited",
                    error_detail=exc.message,
                )
        except Exception:  # noqa: BLE001 - lease expiry recovers the job
            logger.exception("extraction_job_defer_write_failed job_id=%s", job.id)
            return
        logger.info(
            "extraction_job_deferred job_id=%s reason=llm_rate_limited wait_s=%d applied=%s",
            job.id,
            wait,
            deferred,
        )

    async def _mark_target_failed(self, job: ExtractionJob, *, session) -> None:  # noqa: ANN001
        if job.source_kind is ExtractionSourceKind.DOCUMENT_CHUNK:
            if job.target_chunk_id is not None:
                await DocumentChunkRepository(session, user_id=job.user_id).set_extraction_status(
                    chunk_id=job.target_chunk_id, status=ExtractionStatus.FAILED
                )
        elif job.target_message_id is not None and not job.extractor_version.startswith("agent:"):
            await MessageRepository(session, user_id=job.user_id).set_extraction_status(
                message_id=job.target_message_id, status=ExtractionStatus.FAILED
            )

    async def _extract(self, *, window: list[Message], target_message_id: str) -> ExtractionResult:
        """Call the LLM, with one repair attempt on a schema-invalid response."""
        prompt = build_extraction_prompt(window=window, target_message_id=target_message_id)
        return await self._complete_with_repair(prompt)

    async def _extract_document(
        self, *, window: list[DocumentChunk], target_chunk_id: str
    ) -> ExtractionResult:
        """Call the LLM over a document-chunk window -- the document
        analogue of `_extract`, same contract and same repair behavior.
        """
        prompt = build_document_extraction_prompt(window=window, target_chunk_id=target_chunk_id)
        return await self._complete_with_repair(prompt)

    async def _complete_with_repair(self, prompt: str) -> ExtractionResult:
        """One LLM call, with one repair attempt on a schema-invalid response.

        Shared by `_extract`/`_extract_document` -- both build a prompt
        differently but call the LLM identically.
        """
        try:
            return await self._llm.complete_structured(
                prompt=prompt,
                schema=ExtractionResult,
                temperature=0.0,
                timeout=self._settings.llm_timeout_seconds,
            )
        except LLMResponseInvalidError as exc:
            problems = _schema_problems(exc)
            logger.warning("llm_response_invalid attempt=1 problems=%s", problems or "unknown")
            repair_prompt = (
                f"{prompt}\n\n"
                f"Your previous response was rejected: {exc.message}"
                + (f" ({problems})" if problems else "")
                + ". Return ONLY JSON matching the required schema -- no other text."
            )
            return await self._llm.complete_structured(
                prompt=repair_prompt,
                schema=ExtractionResult,
                temperature=0.0,
                timeout=self._settings.llm_timeout_seconds,
            )

    #: Decisions that create a brand-new Memory node (as opposed to DUPLICATE/
    #: UPDATE, which only corroborate or mutate an existing one) -- exactly the
    #: set that needs a fresh embedding afterward.
    _CREATES_NEW_MEMORY = frozenset(
        {
            DecisionType.NEW,
            DecisionType.SUPERSEDING,
            DecisionType.LIKELY_DUPLICATE,
            DecisionType.FLAGGED,
            DecisionType.CONFLICTING,
        }
    )

    #: How many nearest-neighbor existing memories to consider as semantic
    #: evidence per relationship/statement. Small and bounded -- this is a
    #: precision check (does the single best match share entity+predicate
    #: context), not a broad recall exercise.
    _SEMANTIC_EVIDENCE_LIMIT = 5

    #: How many existing entity names get an embedding-similarity comparison per
    #: candidate entity. Deliberately much smaller than `_AMBIGUITY_SCAN_LIMIT`
    #: (500): the canonical-key check there is a string comparison, essentially
    #: free; each comparison here costs one embedding call against a real
    #: provider, so this must stay small to keep per-candidate extraction cost
    #: bounded and predictable.
    _SEMANTIC_ENTITY_SCAN_LIMIT = 20

    async def _apply_candidate(
        self,
        candidate: CandidateMemory,
        *,
        target_content: str,
        source_type: SourceType,
        source_reference: str,
        source_location_prefix: str | None,
        window_text: str,
        user_id: str,
        memory_service: MemoryService,
        retrieval_service: RetrievalService,
        merge_repo: EntityMergeCandidateRepository,
        embedding_repo: EmbeddingRepository,
        db: Database,
        now: datetime,
        stated_at: datetime | None = None,
    ) -> CandidateApplication:
        """Memory-worthiness first (Phase 14, M1: `app.extraction.worthiness`),
        then the unchanged classify-and-persist path. Every recorded reason is
        tagged with the worthiness rule that let the candidate through (or
        stopped it), so "why was this stored / not stored?" has an answer."""
        worth = evaluate_worthiness(candidate, target_text=target_content)
        if not worth.store:
            return CandidateApplication(DecisionType.IGNORE, f"{worth.tag()} {worth.reason}", None)
        # Valid time (Phase 14, M5): when the fact was STATED -- the message's own
        # time -- adjusted by any temporal hint the model reported, never the
        # moment this job happened to run (a deferred job used to date every
        # fact to the day the quota recovered).
        said = stated_at or now
        temporal = interpret_temporal(
            valid_from_hint=candidate.temporal.valid_from_hint,
            valid_until_hint=candidate.temporal.valid_until_hint,
            stated_at=said,
        )
        applied = await self._classify_and_persist(
            candidate,
            temporal=temporal,
            candidate_is_past=temporal.valid_until is not None and temporal.valid_until <= said,
            target_content=target_content,
            source_type=source_type,
            source_reference=source_reference,
            source_location_prefix=source_location_prefix,
            window_text=window_text,
            user_id=user_id,
            memory_service=memory_service,
            retrieval_service=retrieval_service,
            merge_repo=merge_repo,
            embedding_repo=embedding_repo,
            db=db,
            now=now,
        )
        note = f" [temporal: {temporal.note}]" if temporal.note else ""
        return replace(applied, reason=f"{worth.tag()} {applied.reason}{note}")

    async def apply_structured_candidate(
        self,
        candidate: CandidateMemory,
        *,
        stated_at: datetime,
        source_type: SourceType,
        source_reference: str,
        source_location_prefix: str | None,
        user_id: str,
        memory_service: MemoryService,
        retrieval_service: RetrievalService,
        merge_repo: EntityMergeCandidateRepository,
        embedding_repo: EmbeddingRepository,
        db: Database,
        embed_memory: bool,
        supersedes_memory_id: str | None = None,
        new_memory_id: str | None = None,
    ) -> CandidateApplication:
        """Phase 16: the deterministic (no-LLM) entry into the SAME classify-and-persist
        governance every extracted candidate goes through -- grounding, entity
        ambiguity flagging, duplicate / conflict / supersession classification,
        provenance and the canonical write.

        What it skips, and why: the memory-worthiness gate (a structured project
        event is meaningful by construction -- the caller already chose to record
        it) and temporal-hint parsing (valid time is the event's own `stated_at`).
        `embed_memory=False` is for structural facts, which stay searchable by
        keyword and graph without spending embedding quota. Never calls the LLM.
        """
        temporal = interpret_temporal(
            valid_from_hint=None, valid_until_hint=None, stated_at=stated_at
        )
        # Grounding exists to catch an LLM inventing entities that its source never
        # mentioned. A structured event's entities are derived deterministically from
        # its own fields, so the grounding text is the statement plus those names:
        # the check stays in the path (spans, bounds) but cannot reject a real event.
        grounding_text = "\n".join([candidate.statement, *(e.name for e in candidate.entities)])
        return await self._classify_and_persist(
            candidate,
            temporal=temporal,
            candidate_is_past=False,
            target_content=grounding_text,
            source_type=source_type,
            source_reference=source_reference,
            source_location_prefix=source_location_prefix,
            window_text=grounding_text,
            user_id=user_id,
            memory_service=memory_service,
            retrieval_service=retrieval_service,
            merge_repo=merge_repo,
            embedding_repo=embedding_repo,
            db=db,
            now=self._now(),
            embed_memory=embed_memory,
            supersedes_memory_id=supersedes_memory_id,
            new_memory_id=new_memory_id,
        )

    async def _classify_and_persist(
        self,
        candidate: CandidateMemory,
        *,
        temporal: TemporalInterpretation,
        candidate_is_past: bool,
        target_content: str,
        source_type: SourceType,
        source_reference: str,
        source_location_prefix: str | None,
        window_text: str,
        user_id: str,
        memory_service: MemoryService,
        retrieval_service: RetrievalService,
        merge_repo: EntityMergeCandidateRepository,
        embedding_repo: EmbeddingRepository,
        db: Database,
        now: datetime,
        embed_memory: bool = True,
        supersedes_memory_id: str | None = None,
        new_memory_id: str | None = None,
    ) -> CandidateApplication:
        """Classify and, for every outcome but IGNORE/INVALID, persist one candidate.

        `target_content`/`source_type`/`source_reference` generalize what was
        previously a single `target_message: Message` (Phase 6, ADR 0012): a
        conversation message and a document chunk both play the identical
        role here, so the only thing the caller need supply is the target's
        text and where the resulting `Source` should point.

        Never raises: a domain-validation failure downstream (e.g. Phase 1
        rejecting a self-referencing relationship the LLM contract's own checks
        did not catch) is caught and reported as `INVALID` so one bad candidate
        cannot abort the rest of the job.
        """
        grounding = check_grounding(
            candidate,
            target_message_content=target_content,
            window_text=window_text,
        )
        if not grounding.grounded:
            return CandidateApplication(DecisionType.INVALID, grounding.reason, None)

        try:
            await self._flag_ambiguous_entities(
                candidate,
                user_id=user_id,
                retrieval_service=retrieval_service,
                merge_repo=merge_repo,
                now=now,
            )

            per_relationship = []
            for rel in candidate.relationships:
                existing_edges = await self._sibling_edges(
                    source_name=rel.source,
                    predicate=rel.predicate,
                    user_id=user_id,
                    retrieval_service=retrieval_service,
                )
                exact = classify_relationship(
                    predicate=rel.predicate,
                    target_normalized=normalize_name(rel.target),
                    candidate_confidence=candidate.confidence,
                    is_change=candidate.temporal.is_change,
                    replaces_hint=candidate.temporal.replaces_hint,
                    existing_edges=existing_edges,
                    now=now,
                    candidate_is_past=candidate_is_past,
                )
                # Semantic evidence (ADR 0009, PHASE3_PLAN.md section 5) is
                # consulted ONLY when exact-triple matching found nothing to
                # compare against -- never when it already found a different-
                # object edge for this predicate (that is the temporal-change
                # question `classify_relationship` already owns).
                if exact.decision is DecisionType.NEW:
                    exact = await self._semantic_relationship_decision(
                        rel,
                        candidate=candidate,
                        user_id=user_id,
                        embedding_repo=embedding_repo,
                        memory_service=memory_service,
                        fallback=exact,
                    )
                per_relationship.append(exact)
            governing = overall_decision(per_relationship)

            # A relationship-less ("bare statement") candidate has nothing for
            # `overall_decision` to classify against exact edges -- it is always
            # NEW there. Semantic similarity is the only signal that can ever
            # catch a duplicate bare statement (docs/PHASE2_PLAN.md's own stated
            # limitation, closed here).
            if not candidate.relationships and governing.decision is DecisionType.NEW:
                exact = await self._exact_statement_duplicate(
                    candidate, user_id=user_id, memory_service=memory_service
                )
                if exact is not None:
                    governing = exact
            if not candidate.relationships and governing.decision is DecisionType.NEW:
                governing = await self._semantic_bare_statement_decision(
                    candidate,
                    user_id=user_id,
                    embedding_repo=embedding_repo,
                    memory_service=memory_service,
                    fallback=governing,
                )

            if supersedes_memory_id and governing.decision in (
                DecisionType.NEW,
                DecisionType.LIKELY_DUPLICATE,
                DecisionType.FLAGGED,
            ):
                # Phase 16: an EXPLICIT statement ("this replaces decision X") from the
                # caller, resolved to a stored memory by the server. Duplicate and
                # conflict outcomes above still win; this only upgrades a NEW fact.
                governing = RelationshipDecision(
                    DecisionType.SUPERSEDING,
                    "explicit supersession stated by the caller",
                    target_memory_id=supersedes_memory_id,
                )
            memory_id = await self._persist(
                candidate,
                temporal=temporal,
                decision=governing,
                source_type=source_type,
                source_reference=source_reference,
                source_location_prefix=source_location_prefix,
                user_id=user_id,
                memory_service=memory_service,
                new_memory_id=new_memory_id,
            )
            if (
                embed_memory
                and memory_id is not None
                and governing.decision in self._CREATES_NEW_MEMORY
            ):
                await embed_and_store_memory(
                    user_id=user_id,
                    memory_id=memory_id,
                    content=candidate.statement,
                    db=db,
                    embedding_client=self._embeddings,
                    settings=self._settings,
                    now_fn=self._now,
                    id_fn=self._new_id,
                )
            return CandidateApplication(
                governing.decision,
                governing.reason,
                memory_id,
                target_memory_id=governing.target_memory_id,
                evidence_score=governing.evidence_score,
            )
        except AppError as exc:
            if is_infrastructure_error(exc):
                # Phase 11 (J1): a Neo4j/PostgreSQL outage is not a verdict on
                # the candidate. Propagate so the job is retried; recording it
                # as INVALID would drop the memory permanently.
                raise
            logger.warning(
                "Candidate rejected by domain validation for source %s: %s",
                source_reference,
                exc.message,
            )
            return CandidateApplication(
                DecisionType.INVALID, f"rejected during persistence: {exc.message}", None
            )

    async def _semantic_relationship_decision(
        self,
        rel: CandidateRelationship,
        *,
        candidate: CandidateMemory,
        user_id: str,
        embedding_repo: EmbeddingRepository,
        memory_service: MemoryService,
        fallback: RelationshipDecision,
    ) -> RelationshipDecision:
        """Semantic evidence for one relationship that exact matching called NEW.

        This branch is only reached when `_sibling_edges` found NO edge at all for
        `(rel.source, rel.predicate)` -- and `_sibling_edges` looks at every
        outgoing edge from that source entity, not just ones matching this
        candidate. That means an existing memory asserting the *same* predicate
        from the *same* source entity would already have been found there, and
        `classify_relationship` would not have returned NEW in the first place.
        So requiring an exact predicate match again here would make this branch
        unreachable -- there is nothing left for predicate equality to
        discriminate once exact-triple matching has already ruled it out.

        What semantic evidence adds instead is exactly the case exact matching
        structurally cannot see: a *differently-predicated or differently-named*
        restatement of a fact about an entity this candidate already names --
        e.g. the LLM extracting "utilizes" one turn and "uses" the next for an
        otherwise-identical statement. The gate that keeps this conservative is
        not predicate equality (already exhausted) but (a) high full-statement
        similarity and (b) the matched memory actually mentioning this
        relationship's source entity -- not a coincidental textual overlap with
        an unrelated memory. `predicate_compatible` is therefore evaluated as
        "compatible by default once entity context is confirmed": there is no
        remaining predicate signal to check that the exact layer has not already
        used, so it does not gate separately here (see `SemanticEvidence`'s
        module docstring for the general policy this specializes).

        If embedding search errors (e.g. PostgreSQL briefly unavailable), this
        falls back to the exact-match result (`fallback`, always NEW here)
        rather than raising: semantic evidence is an enhancement, and its
        absence must never block ordinary NEW persistence (mirrors core
        invariant 6's "degrade gracefully" for retrieval, applied here to
        extraction).
        """
        try:
            embed_result = await self._embeddings.embed(
                text=candidate.statement, timeout=self._settings.embedding_timeout_seconds
            )
            hits = await embedding_repo.search(
                query_embedding=embed_result.vector,
                embedding_provider=self._settings.embedding_provider,
                embedding_model=self._settings.embedding_model_resolved,
                limit=self._SEMANTIC_EVIDENCE_LIMIT,
            )
        except AppError as exc:
            logger.warning("Semantic evidence lookup failed, falling back to NEW: %s", exc.message)
            return fallback

        source_normalized = normalize_name(rel.source)
        evidence: list[SemanticEvidence] = []
        for hit in hits:
            try:
                detail = await memory_service.get(user_id=user_id, memory_id=hit.memory_id)
            except NotFoundError:
                continue
            shares_source_entity = any(
                e.normalized_name == source_normalized for e in detail.entities
            )
            evidence.append(
                SemanticEvidence(
                    similarity=hit.similarity,
                    same_source_entity=shares_source_entity,
                    predicate_compatible=shares_source_entity,
                    signals_change=candidate.temporal.is_change,
                    candidate_memory_id=hit.memory_id,
                )
            )

        if not evidence:
            return fallback
        return best_semantic_decision(
            evidence,
            duplicate_threshold=self._settings.semantic_duplicate_threshold,
            flag_threshold=self._settings.semantic_flag_threshold,
        )

    async def _exact_statement_duplicate(
        self, candidate: CandidateMemory, *, user_id: str, memory_service: MemoryService
    ) -> RelationshipDecision | None:
        """Phase 14 (M7): a bare statement IDENTICAL to a currently believed
        memory (case, whitespace and final punctuation aside) corroborates it --
        DUPLICATE, a new source on the existing memory -- instead of becoming a
        second active copy flagged as a "likely" duplicate. Identity of text is
        deterministic evidence; semantic similarity alone still never merges."""
        wanted = _statement_key(candidate.statement)
        if not wanted:
            return None
        hits = await memory_service.search(
            user_id=user_id,
            query=candidate.statement.strip().rstrip(".!"),
            statuses=[MemoryStatus.ACTIVE, MemoryStatus.CONFLICTING],
            limit=20,
        )
        for item in hits.items:
            if _statement_key(item.content) == wanted:
                return RelationshipDecision(
                    DecisionType.DUPLICATE,
                    "restates an already-active identical statement",
                    target_memory_id=item.id,
                )
        return None

    async def _semantic_bare_statement_decision(
        self,
        candidate: CandidateMemory,
        *,
        user_id: str,
        embedding_repo: EmbeddingRepository,
        memory_service: MemoryService,
        fallback: RelationshipDecision,
    ) -> RelationshipDecision:
        """Semantic evidence for a relationship-less candidate.

        With no relationship to anchor "source entity"/"predicate", the
        analogous, equally conservative gate is: does the matched existing
        memory mention at least one of the SAME entities this candidate names?
        A high-similarity match against a memory sharing none of this
        candidate's entities is exactly the kind of coincidental textual overlap
        (PHASE3_PLAN.md section 5's "similarity alone is never sufficient") that
        must not be flagged.
        """
        if not candidate.entities:
            return fallback  # nothing to anchor evidence to; stay NEW

        try:
            embed_result = await self._embeddings.embed(
                text=candidate.statement, timeout=self._settings.embedding_timeout_seconds
            )
            hits = await embedding_repo.search(
                query_embedding=embed_result.vector,
                embedding_provider=self._settings.embedding_provider,
                embedding_model=self._settings.embedding_model_resolved,
                limit=self._SEMANTIC_EVIDENCE_LIMIT,
            )
        except AppError as exc:
            logger.warning("Semantic evidence lookup failed, falling back to NEW: %s", exc.message)
            return fallback

        candidate_entity_names = {normalize_name(e.name) for e in candidate.entities}
        evidence: list[SemanticEvidence] = []
        for hit in hits:
            try:
                detail = await memory_service.get(user_id=user_id, memory_id=hit.memory_id)
            except NotFoundError:
                continue
            shares_entity = any(
                e.normalized_name in candidate_entity_names for e in detail.entities
            )
            evidence.append(
                SemanticEvidence(
                    similarity=hit.similarity,
                    same_source_entity=shares_entity,
                    predicate_compatible=shares_entity,  # no predicate to compare; same gate
                    signals_change=candidate.temporal.is_change,
                    candidate_memory_id=hit.memory_id,
                )
            )

        if not evidence:
            return fallback
        return best_semantic_decision(
            evidence,
            duplicate_threshold=self._settings.semantic_duplicate_threshold,
            flag_threshold=self._settings.semantic_flag_threshold,
        )

    async def _sibling_edges(
        self, *, source_name: str, predicate: str, user_id: str, retrieval_service: RetrievalService
    ) -> list[ExistingEdge]:
        """Existing edges sharing this candidate's source entity and predicate.

        Empty (never an error) when the source entity does not exist yet -- a
        brand-new entity trivially has no siblings, which `classify_relationship`
        already treats as NEW.
        """
        try:
            source_entity = await retrieval_service.find_entity_by_name(
                user_id=user_id, name=source_name
            )
        except NotFoundError:
            return []

        views = await retrieval_service.get_relationships(
            user_id=user_id, entity_id=source_entity.id, direction=Direction.OUTGOING
        )
        return [
            ExistingEdge(
                target_normalized_name=view.target_entity.normalized_name,
                status=view.relationship.status,
                memory_id=view.relationship.memory_id,
                confidence=view.relationship.confidence,
            )
            for view in views
            if view.relationship.predicate == predicate and view.relationship.memory_id
        ]

    async def _flag_ambiguous_entities(
        self,
        candidate: CandidateMemory,
        *,
        user_id: str,
        retrieval_service: RetrievalService,
        merge_repo: EntityMergeCandidateRepository,
        now: datetime,
    ) -> None:
        """Scenario G: detect, never merge.

        For each entity the candidate names that does not already exist under its
        exact normalized name, check plausible near-duplicates among existing
        entities and record the suspicion. Both entities are left untouched.

        Two independent signals, both flag-only (PHASE3_PLAN.md section 7):
        canonical-key equality (Phase 2, punctuation/whitespace-insensitive --
        "Open AI"/"OpenAI") and, new in Phase 3, embedding similarity between the
        two *names* -- catching pairs the canonical key does not, such as
        genuine misspellings or abbreviations ("Postgres"/"Postgre SQL"). Neither
        signal, nor both together, ever merges, renames, or shares identity
        between two entities; `EntityMergeCandidate.status` stays `unreviewed`
        until a human decides.
        """
        for entity in candidate.entities:
            if entity.entity_type in EXACT_IDENTITY_TYPES:
                continue  # Phase 16: a path / hash / session key has no "near duplicate"
            try:
                await retrieval_service.find_entity_by_name(user_id=user_id, name=entity.name)
                continue  # exact match already exists -- nothing ambiguous
            except NotFoundError:
                pass

            # A substring CONTAINS search cannot find "OpenAI" from a query of
            # "Open AI" -- the space breaks containment both ways, which is
            # exactly the punctuation/whitespace case ambiguity detection exists
            # to catch. Compare against every existing entity instead (bounded).
            near_matches = await retrieval_service.list_all_entities(
                user_id=user_id, limit=_AMBIGUITY_SCAN_LIMIT
            )
            already_flagged: set[str] = set()
            for other in near_matches:
                if not is_plausible_duplicate(entity.name, other.name):
                    continue
                already_flagged.add(other.id)
                # This candidate's entity does not exist as a Neo4j node yet (it
                # will be created by MemoryService.create() below with a fresh
                # id) -- the flag records the NAME pair via the existing entity's
                # id and a deterministic placeholder for the not-yet-created one,
                # so a reviewer can find it by searching either name.
                await merge_repo.record_candidate(
                    candidate_id=self._new_id(),
                    entity_id_a=other.id,
                    entity_id_b=f"pending:{normalize_name(entity.name)}",
                    evidence={
                        "signal": "canonical_key",
                        "existing_name": other.name,
                        "candidate_name": entity.name,
                        "source_message_id": candidate.source_message_id,
                    },
                    score=None,
                    now=now,
                )

            await self._flag_semantically_similar_entities(
                entity_name=entity.name,
                source_message_id=candidate.source_message_id,
                near_matches=near_matches,
                already_flagged=already_flagged,
                merge_repo=merge_repo,
                now=now,
            )

    async def _flag_semantically_similar_entities(
        self,
        *,
        entity_name: str,
        source_message_id: str,
        near_matches: list[Entity],
        already_flagged: set[str],
        merge_repo: EntityMergeCandidateRepository,
        now: datetime,
    ) -> None:
        """Embedding-similarity ambiguity detection (PHASE3_PLAN.md section 7).

        Compares the candidate entity's *name* against a bounded scan of
        existing entity names via cosine similarity, computed transiently --
        entity names are not persisted to `memory_embeddings` (that table holds
        memory content, not entity names; PHASE3_PLAN.md section 2 makes no
        Neo4j or new-table schema change for entity resolution). A smaller scan
        bound than the canonical-key check's: each comparison here costs one
        embedding call against a real provider, where the canonical-key check
        costs only string comparison, so this must stay far cheaper per
        candidate to keep extraction cost bounded and predictable.
        """
        candidates_to_check = [m for m in near_matches if m.id not in already_flagged][
            : self._SEMANTIC_ENTITY_SCAN_LIMIT
        ]
        if not candidates_to_check:
            return

        try:
            entity_embedding = await self._embeddings.embed(
                text=entity_name, timeout=self._settings.embedding_timeout_seconds
            )
        except AppError as exc:
            logger.warning(
                "Semantic entity-ambiguity check skipped (embedding failed): %s", exc.message
            )
            return

        for other in candidates_to_check:
            try:
                other_embedding = await self._embeddings.embed(
                    text=other.name, timeout=self._settings.embedding_timeout_seconds
                )
            except AppError:
                continue  # one failed comparison must not abort the rest
            similarity = cosine_similarity(entity_embedding.vector, other_embedding.vector)
            if similarity < self._settings.semantic_entity_flag_threshold:
                continue
            await merge_repo.record_candidate(
                candidate_id=self._new_id(),
                entity_id_a=other.id,
                entity_id_b=f"pending:{normalize_name(entity_name)}",
                evidence={
                    "signal": "embedding",
                    "existing_name": other.name,
                    "candidate_name": entity_name,
                    "similarity": similarity,
                    "source_message_id": source_message_id,
                },
                score=similarity,
                now=now,
            )

    async def _persist(
        self,
        candidate: CandidateMemory,
        *,
        temporal: TemporalInterpretation,
        decision: RelationshipDecision,
        source_type: SourceType,
        source_reference: str,
        source_location_prefix: str | None,
        user_id: str,
        memory_service: MemoryService,
        new_memory_id: str | None = None,
    ) -> str | None:
        """Apply the governing decision to Neo4j via the unmodified Phase 1
        `MemoryService`. Every branch below persists or corroborates something, so
        this always returns a memory id; IGNORE and INVALID never reach here.
        """
        span = f"{candidate.source_span.start}:{candidate.source_span.end}"
        location = f"{source_location_prefix}:{span}" if source_location_prefix else span
        source = SourceInput(
            source_type=source_type,
            reference=source_reference,
            location=location,
        )
        entities = [
            EntityInput(name=e.name, entity_type=e.entity_type, aliases=e.aliases)
            for e in candidate.entities
        ]
        relationships = [
            RelationshipInput(
                source=r.source,
                predicate=r.predicate,
                target=r.target,
                confidence=r.confidence,
            )
            for r in candidate.relationships
        ]

        if decision.decision in (
            DecisionType.NEW,
            DecisionType.SUPERSEDING,
            DecisionType.LIKELY_DUPLICATE,
            DecisionType.FLAGGED,
        ):
            # LIKELY_DUPLICATE/FLAGGED (Phase 3, ADR 0009): semantic evidence is
            # never sufficient to merge or overwrite -- both persist exactly like
            # NEW. The match this evidence points at is preserved in the
            # candidate's own audit row (`decision_reason`, `extraction_candidates`
            # -- see the runner's caller), not by mutating Neo4j at all: the
            # approved Phase 3 plan makes no Neo4j schema change, and a possible
            # duplicate is not a fact about the graph, it is a fact about the
            # extraction pipeline's confidence, which is exactly what that audit
            # table exists to record.
            supersedes: list[str] = []
            if decision.decision is DecisionType.SUPERSEDING and decision.target_memory_id:
                supersedes = [decision.target_memory_id]
            payload = MemoryCreate(
                content=candidate.statement,
                memory_type=candidate.memory_type,
                confidence=candidate.confidence,
                entities=entities,
                relationships=relationships,
                source=source,
                supersedes=supersedes,
                valid_from=temporal.valid_from,
                valid_until=temporal.valid_until,
            )
            created = await memory_service.create(
                user_id=user_id,
                payload=payload,
                open_start=temporal.open_start,
                memory_id=new_memory_id,
            )
            return created.memory.id

        if decision.decision is DecisionType.CONFLICTING:
            payload = MemoryCreate(
                content=candidate.statement,
                memory_type=candidate.memory_type,
                status=MemoryStatus.CONFLICTING,
                confidence=candidate.confidence,
                entities=entities,
                relationships=relationships,
                source=source,
                valid_from=temporal.valid_from,
                valid_until=temporal.valid_until,
            )
            created = await memory_service.create(
                user_id=user_id,
                payload=payload,
                open_start=temporal.open_start,
                memory_id=new_memory_id,
            )
            if decision.target_memory_id:
                await memory_service.link_conflicting(
                    user_id=user_id,
                    memory_id_a=created.memory.id,
                    memory_id_b=decision.target_memory_id,
                )
            return created.memory.id

        if decision.decision is DecisionType.UPDATE:
            assert decision.target_memory_id is not None  # UPDATE always names a target
            await memory_service.update(
                user_id=user_id,
                memory_id=decision.target_memory_id,
                payload=MemoryUpdate(confidence=candidate.confidence),
            )
            detail = await memory_service.attach_source(
                user_id=user_id,
                memory_id=decision.target_memory_id,
                source_type=source.source_type,
                reference=source.reference,
                location=source.location,
            )
            return detail.memory.id

        # DUPLICATE
        assert decision.target_memory_id is not None  # DUPLICATE always names a target
        detail = await memory_service.attach_source(
            user_id=user_id,
            memory_id=decision.target_memory_id,
            source_type=source.source_type,
            reference=source.reference,
            location=source.location,
        )
        return detail.memory.id


async def run_worker_tick(
    *,
    db: Database,
    graph_client: GraphClient,
    llm_client: LLMClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    worker_id: str,
    limit: int = 5,
    now_fn: Callable[[], datetime] = _utcnow,
) -> int:
    """Claim up to `limit` jobs and process each to completion.

    Claiming happens in its own short transaction that commits immediately
    (marking the jobs `in_progress`); each job is then processed in a fresh
    session. This is deliberate: if the process dies partway through the loop, the
    jobs already claimed stay correctly `in_progress` with a lease, rather than
    the whole claim rolling back with them (docs/PHASE2_PLAN.md section 8).

    Used both by the live per-message background task and by the sweep -- the
    only difference between them is `limit` and how jobs got into the claimable
    state in the first place.
    """
    now = now_fn()
    # A lease token unique to THIS claim (Phase 11, J4): several ticks may share
    # a `worker_id` (e.g. "sweep"), and ownership checks are only meaningful
    # if two claims can never present the same token.
    lease_token = f"{worker_id}:{uuid.uuid4().hex[:12]}"
    async with db.session() as session:
        claimed = await ExtractionQueue(session).claim_batch(
            limit=limit,
            worker_id=lease_token,
            now=now,
            max_attempts=settings.extraction_max_attempts,
        )

    if not claimed:
        return 0

    runner = ExtractionRunner(
        graph_client=graph_client,
        llm_client=llm_client,
        embedding_client=embedding_client,
        settings=settings,
        now_fn=now_fn,
    )
    for job in claimed:
        await runner.run(job, db=db)
    return len(claimed)


async def run_sweep_tick(
    *,
    db: Database,
    graph_client: GraphClient,
    llm_client: LLMClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    worker_id: str = "sweep",
    now_fn: Callable[[], datetime] = _utcnow,
) -> dict[str, int]:
    """The bounded, idempotent periodic reconciliation (docs/adr/0008).

    Three things, each bounded by configuration and safe to run repeatedly:

    1. Reclaim jobs whose worker died mid-run (expired lease).
    2. Re-evaluate the *window* gate for messages the live-path gate skipped --
       context that did not exist yet when a message first arrived may make it
       meaningful in retrospect.
    3. A normal worker tick, sized for the backlog rather than a single message.

    Returns counts for observability; call sites (the internal endpoint,
    scripts/run_sweep.py) log or display them.
    """
    now = now_fn()

    async with db.session() as session:
        reclaimed = await ExtractionQueue(session).reclaim_expired_leases(
            lease_seconds=settings.extraction_lease_seconds,
            max_attempts=settings.extraction_max_attempts,
            now=now,
        )

    # Phase 11 (J5/J7): retry exhaustion is explicit, and an abandoned job's
    # message/chunk leaves its non-terminal state so documents can finalize.
    async with db.session() as session:
        queue = ExtractionQueue(session)
        abandoned = await queue.abandon_exhausted(
            max_attempts=settings.extraction_max_attempts, now=now
        )
        failed_messages, touched_documents = await queue.fail_targets_of_abandoned_jobs()
    for user_id, document_id in touched_documents:
        await finalize_document(db=db, user_id=user_id, document_id=document_id, now=now)

    rechecked = 0
    if settings.extraction_recheck_skipped:
        async with db.session() as session:
            skipped = await ExtractionQueue(session).list_skipped_for_recheck(
                lookback_days=settings.extraction_sweep_lookback_days,
                limit=settings.extraction_sweep_batch_size,
                now=now,
            )
        for message in skipped:
            async with db.session() as session:
                await maybe_trigger_extraction(
                    message=message,
                    message_repo=MessageRepository(session, user_id=message.user_id),
                    job_repo=ExtractionJobRepository(session, user_id=message.user_id),
                    settings=settings,
                    now=now,
                )
            rechecked += 1

    processed = await run_worker_tick(
        db=db,
        graph_client=graph_client,
        llm_client=llm_client,
        embedding_client=embedding_client,
        settings=settings,
        worker_id=worker_id,
        limit=settings.extraction_sweep_batch_size,
        now_fn=now_fn,
    )
    return {
        "reclaimed_leases": reclaimed,
        "abandoned_exhausted": abandoned,
        "failed_targets": failed_messages + len(touched_documents),
        "rechecked_skipped": rechecked,
        "jobs_processed": processed,
    }
