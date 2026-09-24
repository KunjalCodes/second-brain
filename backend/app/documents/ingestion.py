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
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import StorageUnavailableError
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
        """Never raises: every failure path records itself on the job and
        returns, mirroring `ExtractionRunner.run`'s exact contract.
        """
        now = self._now()
        async with db.session() as session:
            document = await DocumentRepository(session, user_id=job.user_id).get(
                document_id=job.document_id
            )
        if document is None:
            async with db.session() as session:
                await IngestionQueue(session).mark_failed(
                    job_id=job.id,
                    error_code="document_missing",
                    error_detail="the job's document was not found",
                    permanent=True,
                    next_attempt_at=None,
                    now=now,
                )
            return

        try:
            parsed_chunks_count = await self._validate_parse_chunk(
                document, job=job, db=db, now=now
            )
        except DocumentParseError as exc:
            # Never mark COMPLETE when a required stage failed (PHASE6_PLAN.md
            # section 5): the document's own status is FAILED, explicitly,
            # with the parser's stable error_code -- never silently absorbed.
            async with db.session() as session:
                await DocumentRepository(session, user_id=job.user_id).set_status(
                    document_id=document.id,
                    status=DocumentStatus.FAILED,
                    error_code=exc.error_code,
                    error_detail=exc.message,
                )
                await IngestionQueue(session).mark_failed(
                    job_id=job.id,
                    error_code=exc.error_code,
                    error_detail=exc.message,
                    permanent=True,
                    next_attempt_at=None,
                    now=now,
                )
            return
        except TimeoutError:
            async with db.session() as session:
                await DocumentRepository(session, user_id=job.user_id).set_status(
                    document_id=document.id,
                    status=DocumentStatus.FAILED,
                    error_code="parse_timeout",
                    error_detail=f"Parsing did not finish within "
                    f"{self._settings.ingestion_parse_timeout_seconds}s",
                )
                await IngestionQueue(session).mark_failed(
                    job_id=job.id,
                    error_code="parse_timeout",
                    error_detail="Parsing timed out",
                    permanent=False,
                    next_attempt_at=now + _retry_backoff(job.attempts),
                    now=now,
                )
            return
        except StorageUnavailableError as exc:
            async with db.session() as session:
                await IngestionQueue(session).mark_failed(
                    job_id=job.id,
                    error_code="storage_unavailable",
                    error_detail=exc.message,
                    permanent=False,
                    next_attempt_at=now + _retry_backoff(job.attempts),
                    now=now,
                )
            return
        except Exception as exc:  # noqa: BLE001 - never let one bad document crash a worker tick
            logger.exception("Unexpected error ingesting document %s", document.id)
            async with db.session() as session:
                await IngestionQueue(session).mark_failed(
                    job_id=job.id,
                    error_code="internal_error",
                    error_detail=str(exc),
                    permanent=False,
                    next_attempt_at=now + _retry_backoff(job.attempts),
                    now=now,
                )
            return

        async with db.session() as session:
            await IngestionQueue(session).mark_done(job_id=job.id, now=now)

        logger.info(
            "ingestion_job_done job_id=%s document_id=%s chunk_count=%d",
            job.id,
            document.id,
            parsed_chunks_count,
        )

    async def _validate_parse_chunk(
        self, document: Document, *, job: IngestionJob, db: Database, now: datetime
    ) -> int:
        """VALIDATING -> PARSING -> CHUNKING, then persist chunks and create
        one `ExtractionJob` per chunk. Returns the chunk count. Raises
        `DocumentParseError` for any stage-specific rejection (mapped by the
        caller to a FAILED document, never absorbed).
        """
        async with db.session() as session:
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id, status=DocumentStatus.VALIDATING
            )
        data = await self._storage.get(storage_key=document.storage_key, user_id=job.user_id)

        async with db.session() as session:
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id, status=DocumentStatus.PARSING
            )
        parsed = await asyncio.wait_for(
            asyncio.to_thread(
                parse_document,
                data,
                content_type=document.content_type,
                max_pdf_pages=self._settings.max_document_pages,
                min_pdf_text_chars_per_page=self._settings.document_min_text_chars_per_page,
            ),
            timeout=self._settings.ingestion_parse_timeout_seconds,
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
            # Idempotent retry safety: a previous, interrupted attempt at
            # this same job may have already written some chunks -- clear
            # them before writing the freshly (deterministically -- section
            # 8) computed set, rather than risking a duplicate ordinal.
            await chunk_repo.delete_all_for_document(document_id=document.id)
            await chunk_repo.bulk_create(chunks=chunk_rows)

            status = DocumentStatus.CHUNKED if chunk_rows else DocumentStatus.COMPLETE
            await DocumentRepository(session, user_id=job.user_id).set_status(
                document_id=document.id,
                status=status,
                page_count=parsed.page_count,
                processed_at=now if not chunk_rows else None,
            )

        if chunk_rows:
            async with db.session() as session:
                job_repo = ExtractionJobRepository(session, user_id=job.user_id)
                for row in chunk_rows:
                    window_start = max(0, row.ordinal - 1)
                    window_end = row.ordinal + 1
                    await job_repo.create_document_chunk_job(
                        job_id=self._new_id(),
                        document_id=document.id,
                        target_chunk_id=row.id,
                        window_start_ordinal=window_start,
                        window_end_ordinal=window_end,
                        extractor_version=self._settings.document_extractor_version,
                        now=now,
                    )
                await DocumentRepository(session, user_id=job.user_id).set_status(
                    document_id=document.id, status=DocumentStatus.EXTRACTING
                )

        return len(chunk_rows)


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
    mirrors `app.extraction.runner.run_worker_tick` exactly.
    """
    now = now_fn()
    async with db.session() as session:
        claimed = await IngestionQueue(session).claim_batch(
            limit=limit,
            worker_id=worker_id,
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
    `app.extraction.runner.run_sweep_tick`'s two-part shape (reclaim expired
    leases, then a normal worker tick sized for the backlog).
    """
    now = now_fn()
    async with db.session() as session:
        reclaimed = await IngestionQueue(session).reclaim_expired_leases(
            lease_seconds=settings.ingestion_lease_seconds,
            max_attempts=settings.ingestion_max_attempts,
            now=now,
        )

    processed = await run_ingestion_worker_tick(
        db=db,
        storage=storage,
        settings=settings,
        worker_id=worker_id,
        limit=settings.ingestion_sweep_batch_size,
        now_fn=now_fn,
    )
    return {"reclaimed_leases": reclaimed, "jobs_processed": processed}
