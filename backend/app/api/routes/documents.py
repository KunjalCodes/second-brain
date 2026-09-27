"""Document upload and inspection endpoints (Phase 6, PHASE6_PLAN.md
section 2/3).

Handlers do no business logic: they bind HTTP to `DocumentService` and
nothing else, matching `app/api/routes/conversations.py`. `upload_document`
schedules ingestion (then extraction) as `BackgroundTask`s, registered before
the response is returned but not run until after it is sent -- so parsing a
large document, or a slow LLM provider, never delays the upload response
(docs/adr/0008's pattern, reused unchanged for documents).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Query, UploadFile, status

from app.api.deps import (
    RATE_LIMIT_INGEST,
    CurrentUser,
    DbSessionDep,
    DocumentServiceDep,
    DocumentStorageDep,
    EmbeddingClientDep,
    LLMClientDep,
    SettingsDep,
)
from app.core.config import Settings
from app.core.errors import ValidationError
from app.db.engine import get_database
from app.documents.ingestion import run_ingestion_worker_tick
from app.embeddings.base import EmbeddingClient
from app.extraction.runner import run_worker_tick
from app.graph.connection import get_graph_client
from app.llm.base import LLMClient
from app.schemas.document import (
    DocumentChunkListResponse,
    DocumentChunkResponse,
    DocumentListResponse,
    DocumentResponse,
)
from app.storage.base import DocumentStorage

router = APIRouter(prefix="/documents", tags=["documents"])
logger = logging.getLogger(__name__)

#: Mirrors `app.api.routes.conversations._LIVE_TICK_LIMIT`: the live,
#: per-upload background tick is deliberately small. A large backlog is
#: drained by the sweep (INGESTION_SWEEP_BATCH_SIZE / EXTRACTION_SWEEP_BATCH_SIZE),
#: not by this one.
_LIVE_TICK_LIMIT = 3


async def _background_ingest_and_extract(
    *,
    storage: DocumentStorage,
    llm_client: LLMClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    worker_id: str,
) -> None:
    """Runs after the HTTP response has been sent. Never raises into the
    request/response cycle -- any failure here was already recorded on the
    ingestion/extraction job itself; an exception escaping this function
    would only be logged by the ASGI server, not surfaced to a client who
    already has their response (mirrors
    `conversations._background_extraction_tick` exactly).

    Ingestion runs first and, on success, hands off to per-chunk
    `ExtractionJob` rows (ADR 0012) -- so one background task chains both
    ticks, the same way a single upload conceptually triggers one pipeline.
    """
    try:
        ingested = await run_ingestion_worker_tick(
            db=get_database(),
            storage=storage,
            settings=settings,
            worker_id=worker_id,
            limit=_LIVE_TICK_LIMIT,
        )
        if ingested:
            logger.info("Background ingestion tick processed %d job(s)", ingested)
    except Exception:  # noqa: BLE001 - background task; must not raise further
        logger.exception("Background ingestion tick failed")
        return

    try:
        extracted = await run_worker_tick(
            db=get_database(),
            graph_client=get_graph_client(),
            llm_client=llm_client,
            embedding_client=embedding_client,
            settings=settings,
            worker_id=worker_id,
            limit=_LIVE_TICK_LIMIT,
        )
        if extracted:
            logger.info("Background extraction tick processed %d job(s)", extracted)
    except Exception:  # noqa: BLE001 - background task; must not raise further
        logger.exception("Background extraction tick failed")


_READ_CHUNK_BYTES = 256 * 1024


async def _read_bounded(file: UploadFile, *, limit: int) -> bytes:
    """Read the upload in chunks, refusing it as soon as it passes `limit`
    (Phase 11, S10) -- the whole file is never loaded just to find out it is
    too large. One byte past the limit is enough to know."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise ValidationError(
                f"The uploaded file exceeds the limit of {limit} bytes",
                details={"error_code": "file_too_large"},
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "",
    dependencies=[RATE_LIMIT_INGEST],
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
)
async def upload_document(
    user_id: CurrentUser,
    service: DocumentServiceDep,
    db_session: DbSessionDep,
    storage: DocumentStorageDep,
    background_tasks: BackgroundTasks,
    llm_client: LLMClientDep,
    embedding_client: EmbeddingClientDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    content = await _read_bounded(file, limit=settings.max_document_size_bytes)
    document, is_new = await service.upload_document(
        user_id=user_id, filename=file.filename or "untitled", content=content
    )
    if is_new:
        # FastAPI/Starlette runs a `yield`-dependency's exit code (which is
        # where `get_db_session`'s `async with db.session()` commits) AFTER
        # `BackgroundTasks`, not before -- documented FastAPI behavior, not a
        # bug in this app. Left alone, the background tick below would query
        # for a claimable `IngestionJob` before this request's own insert of
        # it was ever committed, and silently find nothing (caught by the
        # tests that exercise this through the real HTTP layer, not just the
        # service/runner directly). Committing explicitly here, before
        # scheduling the task, closes that race; `get_db_session`'s own
        # commit-on-exit afterward is then a safe no-op (nothing left
        # pending).
        await db_session.commit()
        background_tasks.add_task(
            _background_ingest_and_extract,
            storage=storage,
            llm_client=llm_client,
            embedding_client=embedding_client,
            settings=settings,
            worker_id=f"live:{document.id}",
        )
    return DocumentResponse.from_domain(document)


@router.get("", response_model=DocumentListResponse, summary="List documents")
async def list_documents(
    user_id: CurrentUser,
    service: DocumentServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    documents = await service.list_documents(
        user_id=user_id, limit=min(limit, settings.search_max_limit), offset=offset
    )
    return DocumentListResponse(documents=[DocumentResponse.from_domain(d) for d in documents])


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get a document's status and metadata",
)
async def get_document(
    document_id: str,
    user_id: CurrentUser,
    service: DocumentServiceDep,
) -> DocumentResponse:
    document = await service.get_document(user_id=user_id, document_id=document_id)
    return DocumentResponse.from_domain(document)


@router.get(
    "/{document_id}/chunks",
    response_model=DocumentChunkListResponse,
    summary="List a document's chunks",
)
async def list_document_chunks(
    document_id: str,
    user_id: CurrentUser,
    service: DocumentServiceDep,
) -> DocumentChunkListResponse:
    chunks = await service.list_chunks(user_id=user_id, document_id=document_id)
    return DocumentChunkListResponse(chunks=[DocumentChunkResponse.from_domain(c) for c in chunks])
