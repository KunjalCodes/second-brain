"""The ingestion pipeline: VALIDATING -> PARSING -> CHUNKING (Phase 6,
PHASE6_PLAN.md section 2). Mirrors `app/extraction/runner.py`'s own
claim/lease/retry shape and "never raises, always records" contract,
applied to a different unit of work.

Chunk-level `ExtractionJob` rows are created as this pipeline's last step
(one per chunk, via `ExtractionJobRepository.create_document_chunk_job`) --
this is where ingestion hands off to the *unchanged* extraction pipeline
(ADR 0012). Actually running those jobs is `run_worker_tick`'s job, not
this module's; the API route chains the two ticks in one background task
(`app/api/routes/documents.py`), matching how the conversation path already
chains message-append (sync) with a background extraction tick.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    AppError,
    NotFoundError,
    StorageUnavailableError,
    is_infrastructure_error,
)
from app.core.logging import bind_job, reset_job
from app.db.engine import Database
from app.db.models import DocumentChunk as ChunkRow
from app.documents.chunking import chunk_document
from app.documents.parsers import parse_document
from app.documents.parsers.base import DocumentParseError
from app.models.conversation import ExtractionStatus
from app.models.document import Document, DocumentStatus, IngestionJob
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.extraction_repository import ExtractionJobRepository
from app.repositories.ingestion_queue import IngestionQueue
from app.storage.base import DocumentStorage

logger = logging.getLogger(__name__)

#: Same backoff shape as `app/extraction/runner.py`'s `_retry_backoff`.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600


def _retry_backoff(attempts: int) -> timedelta:
    seconds = min(_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), _BACKOFF_CAP_SECONDS)
    return timedelta(seconds=seconds)


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: At most ONE parser thread may be alive at a time (Phase 11, J8).
#: `asyncio.wait_for(asyncio.to_thread(...))` enforces the *job's* deadline but
#: cannot stop the thread: pypdf has no cancellation hook and CPython cannot
#: kill a thread. A timed-out parse therefore keeps running until pypdf
#: returns. The slot is acquired before the thread starts and released by the
#: thread itself when parsing truly finishes, so a runaway parse can never
#: be joined by a second one; other jobs are deferred (no attempt consumed)
#: until the slot frees. True cancellation would need a process boundary --
#: documented as a limitation, not claimed.
_PARSE_SLOT = threading.Lock()
_PARSER_BUSY_RETRY_SECONDS = 30


class ParserBusyError(Exception):
    """The single parser slot is still held by an earlier (possibly timed-out) parse."""


def _parse_holding_slot(data: bytes, **kwargs):  # noqa: ANN003, ANN202
    try:
        return parse_document(data, **kwargs)
    finally:
        _PARSE_SLOT.release()


class IngestionRunner:
    """Processes exactly one claimed ingestion job per `run` call -- the
    document analogue of `ExtractionRunner`.
    """

    def __init__(
        self,
        *,
        storage: DocumentStorage,
        settings: Settings,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._storage = storage
        self._settings = settings
        self._now = now_fn
        self._new_id = id_fn

    async def run(self, job: IngestionJob, *, db: Database) -> None:
        """Process one job with `job_id` bound into every log line (Phase 11, M5)."""
        tokens = bind_job(job.id, "ingestion")
        try:
            await self._run(job, db=db)
        finally:
            reset_job(tokens)

    async def _run(self, job: IngestionJob, *, db: Database) -> None:
        """Never raises: every failure path records itself on the job and
        returns, mirroring `ExtractionRunner.run`'s exact contract.

        Every completion write is conditional on still owning the lease
        (`job.locked_by`, Phase 11, J4).
        """
        now = self._now()
        owner = job.locked_by
        try:
            async with db.session() as session:
                document = await DocumentRepository(session, user_id=job.user_id).get(
                    document_id=job.document_id
                )
                if owner is not None and not await IngestionQueue(session).renew_lease(
                    job_id=job.id, owner=owner, now=now
                ):
                    logger.warning("ingestion_job_lease_lost job_id=%s owner=%s", job.id, owner)
                    return
        except Exception:  # noqa: BLE001 - lease expiry recovers the job
            logger.exception("ingestion_job_start_failed job_id=%s", job.id)
            return
        if document is None:
            await self._fail(
                job,
                db=db,
                now=now,
                error_code="document_missing",
                error_detail="the job's document was not found",
                permanent=True,
            )
            return

        try:
            parsed_chunks_count = await self._validate_parse_chunk(
                document, job=job, db=db, now=now
            )
        except ParserBusyError:
            async with db.session() as session:
                if owner is not None:
                    await IngestionQueue(session).defer(
                        job_id=job.id,
                        owner=owner,
                        until=now + timedelta(seconds=_PARSER_BUSY_RETRY_SECONDS),
                        error_code="parser_busy",
                        error_detail="another document is still being parsed",
                    )
            return
        except DocumentParseError as exc:
            # Never mark COMPLETE when a required stage failed (PHASE6_PLAN.md
            # section 5): the document's own status is FAILED, explicitly,
            # with the parser's stable error_code -- never silently absorbed.
            await self._fail(
                job,
                db=db,
                now=now,
                error_code=exc.error_code,
                error_detail=exc.message,
                permanent=True,
                document_id=document.id,
            )
            return
        except TimeoutError:
            # Terminal and consistent (Phase 11, J9): the document is FAILED and
            # the job abandoned. Re-parsing the same bytes would almost surely
            # time out again while the previous parse still holds the slot.
            await self._fail(
                job,
                db=db,
                now=now,
                error_code="parse_timeout",
                error_detail=(
                    f"Parsing did not finish within "
                    f"{self._settings.ingestion_parse_timeout_seconds}s"
                ),
                permanent=True,
                document_id=document.id,
            )
            return
        except NotFoundError:
            # Transient storage (docs/PHASE11_PLAN.md section 16): the bytes
            # did not survive a restart before ingestion finished. Terminal;
            # the user uploads the file again.
            await self._fail(
                job,
                db=db,
                now=now,
                error_code="document_missing",
                error_detail="The uploaded file is no longer available; upload it again",
                permanent=True,
                document_id=document.id,
            )
            return
        except StorageUnavailableError as exc:
            await self._fail(
                job, db=db, now=now, error_code="storage_unavailable", error_detail=exc.message
            )
            return
        except AppError as exc:
            if not is_infrastructure_error(exc):
                logger.exception("ingestion_job_unexpected_error job_id=%s", job.id)
            await self._fail(
                job,
                db=db,
                now=now,
                error_code=exc.code if is_infrastructure_error(exc) else "internal_error",
                error_detail=exc.message
                if is_infrastructure_error(exc)
                else f"unexpected {type(exc).__name__}",
            )
            return
        except Exception as exc:  # noqa: BLE001 - never let one bad document crash a worker tick
            logger.exception("ingestion_job_unexpected_error job_id=%s", job.id)
            await self._fail(
                job,
                db=db,
                now=now,
                error_code="internal_error",
                error_detail=f"unexpected {type(exc).__name__}",
            )
            return

        try:
            async with db.session() as session:
                done = await IngestionQueue(session).mark_done(job_id=job.id, now=now, owner=owner)
        except Exception:  # noqa: BLE001 - lease expiry recovers the job
            logger.exception("ingestion_job_completion_write_failed job_id=%s", job.id)
            return
        if not done:
            logger.warning(
                "ingestion_job_lease_lost job_id=%s owner=%s stage=complete", job.id, owner
            )
            return
        if parsed_chunks_count and self._settings.document_delete_bytes_after_ingestion:
            # Staging storage policy (docs/PHASE11_PLAN.md section 16): the raw
            # bytes are only needed until chunks exist; provenance lives in
            # PostgreSQL. Best effort -- a failure here loses nothing.
            try:
                await self._storage.delete(storage_key=document.storage_key, user_id=job.user_id)
            except Exception:  # noqa: BLE001
                logger.warning("ingestion_bytes_delete_failed document_id=%s", document.id)

        logger.info(
            "ingestion_job_done job_id=%s document_id=%s chunk_count=%d",
            job.id,
            document.id,
            parsed_chunks_count,
        )

    async def _fail(
        self,
        job: IngestionJob,
        *,
        db: Database,
        now: datetime,
        error_code: str,
        error_detail: str,
        permanent: bool = False,
        document_id: str | None = None,
    ) -> None:
        """Record a failure on an owned job; never raises. Retry exhaustion is
        terminal immediately (Phase 11, J5); a terminal failure marks the
        document FAILED."""
        exhausted = not permanent and job.attempts >= self._settings.ingestion_max_attempts
        if exhausted:
            error_detail = f"retries exhausted after {job.attempts} attempts: {error_detail}"
        terminal = permanent or exhausted
        try:
            async with db.session() as session:
                applied = await IngestionQueue(session).mark_failed(
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
                        "ingestion_job_lease_lost job_id=%s owner=%s stage=fail",
                        job.id,
                        job.locked_by,
                    )
                    return
                if terminal and (document_id or job.document_id):
                    await DocumentRepository(session, user_id=job.user_id).set_status(
                        document_id=document_id or job.document_id,
                        status=DocumentStatus.FAILED,
                        error_code=error_code,
                        error_detail=error_detail,
                    )
        except Exception:  # noqa: BLE001 - lease expiry recovers the job
            logger.exception("ingestion_job_failure_write_failed job_id=%s", job.id)

    async def _validate_parse_chunk(
        self, document: Document, *, job: IngestionJob, db: Database, now: datetime
    ) -> int:
        """VALIDATING -> PARSING -> CHUNKING, then persist chunks and create
        one `ExtractionJob` per chunk. Returns the chunk count. Raises
        `DocumentParseError` for any stage-specific rejection (mapped by the
        caller to a FAILED document, never absorbed).

        Resumable (Phase 11, J6): chunks are written atomically in one
        transaction, so if they already exist a previous attempt got past
        chunking and crashed later. The retry reuses those chunks and only
        ensures their (idempotent) extraction jobs exist. It never deletes
        chunks that extraction jobs already reference -- which the FK on
        `extraction_jobs.target_chunk_id` would (correctly) refuse -- and it
        does not need the raw bytes again.
        """
        async with db.session() as session:
            existing = await DocumentChunkRepository(
                session, user_id=job.user_id
            ).list_for_document(document_id=document.id)
        if existing:
            logger.info(
                "ingestion_job_resume job_id=%s document_id=%s chunks=%d",
                job.id,
                document.id,
                len(existing),
            )
            await self._ensure_chunk_jobs(
                document_id=document.id,
                user_id=job.user_id,
                chunks=[(c.id, c.ordinal) for c in existing],
                db=db,
                now=now,
            )
            return len(existing)

        async with db.session() as session:
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id, status=DocumentStatus.VALIDATING
            )
        data = await self._storage.get(storage_key=document.storage_key, user_id=job.user_id)

        async with db.session() as session:
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id, status=DocumentStatus.PARSING
            )
        if not _PARSE_SLOT.acquire(blocking=False):
            raise ParserBusyError()
        try:
            parse_thread = asyncio.to_thread(
                _parse_holding_slot,
                data,
                content_type=document.content_type,
                max_pdf_pages=self._settings.max_document_pages,
                min_pdf_text_chars_per_page=self._settings.document_min_text_chars_per_page,
            )
        except BaseException:
            _PARSE_SLOT.release()
            raise
        parsed = await asyncio.wait_for(
            parse_thread, timeout=self._settings.ingestion_parse_timeout_seconds
        )

        async with db.session() as session:
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id, status=DocumentStatus.CHUNKING
            )
        chunk_specs = chunk_document(
            parsed,
            chunk_size_chars=self._settings.document_chunk_size_chars,
            overlap_chars=self._settings.document_chunk_overlap_chars,
            header_footer_threshold=self._settings.document_header_footer_repetition_threshold,
            max_chunks=self._settings.max_chunks_per_document,
        )

        chunk_rows = [
            ChunkRow(
                id=self._new_id(),
                document_id=document.id,
                user_id=job.user_id,
                ordinal=spec.ordinal,
                content=spec.content,
                content_hash=spec.content_hash,
                page_start=spec.page_start,
                page_end=spec.page_end,
                heading_context=spec.heading_context,
                char_start=spec.char_start,
                char_end=spec.char_end,
                extraction_status=ExtractionStatus.PENDING.value,
                created_at=now,
            )
            for spec in chunk_specs
        ]

        async with db.session() as session:
            chunk_repo = DocumentChunkRepository(session, user_id=job.user_id)
            await chunk_repo.bulk_create(chunks=chunk_rows)
            status = DocumentStatus.CHUNKED if chunk_rows else DocumentStatus.COMPLETE
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id,
                status=status,
                page_count=parsed.page_count,
                processed_at=now if not chunk_rows else None,
            )

        if chunk_rows:
            await self._ensure_chunk_jobs(
                document_id=document.id,
                user_id=job.user_id,
                chunks=[(row.id, row.ordinal) for row in chunk_rows],
                db=db,
                now=now,
            )
        return len(chunk_rows)

    async def _ensure_chunk_jobs(
        self,
        *,
        document_id: str,
        user_id: str,
        chunks: list[tuple[str, int]],
        db: Database,
        now: datetime,
    ) -> None:
        """One extraction job per chunk; idempotent via the
        `(target_chunk_id, extractor_version)` unique key."""
        async with db.session() as session:
            job_repo = ExtractionJobRepository(session, user_id=user_id)
            for chunk_id, ordinal in chunks:
                await job_repo.create_document_chunk_job(
                    job_id=self._new_id(),
                    document_id=document_id,
                    target_chunk_id=chunk_id,
                    window_start_ordinal=max(0, ordinal - 1),
                    window_end_ordinal=ordinal + 1,
                    extractor_version=self._settings.document_extractor_version,
                    now=now,
                )
            document = await DocumentRepository(session, user_id=user_id).get(
                document_id=document_id
            )
            if document is not None and document.status in (
                DocumentStatus.CHUNKED,
                DocumentStatus.VALIDATING,
                DocumentStatus.PARSING,
                DocumentStatus.CHUNKING,
            ):
                await DocumentRepository(session, user_id=user_id).set_status(
                    document_id=document_id, status=DocumentStatus.EXTRACTING
                )


async def run_ingestion_worker_tick(
    *,
    db: Database,
    storage: DocumentStorage,
    settings: Settings,
    worker_id: str,
    limit: int = 5,
    now_fn: Callable[[], datetime] = _utcnow,
) -> int:
    """Claim up to `limit` ingestion jobs and process each to completion --
    mirrors `app.extraction.runner.run_worker_tick` exactly (including the
    per-claim lease token).
    """
    now = now_fn()
    lease_token = f"{worker_id}:{uuid.uuid4().hex[:12]}"
    async with db.session() as session:
        claimed = await IngestionQueue(session).claim_batch(
            limit=limit,
            worker_id=lease_token,
            now=now,
            max_attempts=settings.ingestion_max_attempts,
        )
    if not claimed:
        return 0

    runner = IngestionRunner(storage=storage, settings=settings, now_fn=now_fn)
    for job in claimed:
        await runner.run(job, db=db)
    return len(claimed)


async def run_ingestion_sweep_tick(
    *,
    db: Database,
    storage: DocumentStorage,
    settings: Settings,
    worker_id: str = "ingestion-sweep",
    now_fn: Callable[[], datetime] = _utcnow,
) -> dict[str, int]:
    """Bounded, idempotent periodic reconciliation -- mirrors
    `app.extraction.runner.run_sweep_tick`: reclaim expired leases, make
    retry exhaustion explicit, fail documents whose job was abandoned, then a
    normal worker tick sized for the backlog.
    """
    now = now_fn()
    async with db.session() as session:
        queue = IngestionQueue(session)
        reclaimed = await queue.reclaim_expired_leases(
            lease_seconds=settings.ingestion_lease_seconds,
            max_attempts=settings.ingestion_max_attempts,
            now=now,
        )
        abandoned = await queue.abandon_exhausted(
            max_attempts=settings.ingestion_max_attempts, now=now
        )
        failed_documents = await queue.fail_documents_of_abandoned_jobs()

    processed = await run_ingestion_worker_tick(
        db=db,
        storage=storage,
        settings=settings,
        worker_id=worker_id,
        limit=settings.ingestion_sweep_batch_size,
        now_fn=now_fn,
    )
    return {
        "reclaimed_leases": reclaimed,
        "abandoned_exhausted": len(abandoned),
        "failed_documents": failed_documents,
        "jobs_processed": processed,
    }
