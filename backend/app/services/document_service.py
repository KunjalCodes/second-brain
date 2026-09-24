"""Document upload and inspection business logic (Phase 6, PHASE6_PLAN.md
section 2/3).

Mirrors `ConversationService`'s split between synchronous and asynchronous
work: this service owns cheap, synchronous bookkeeping -- validate, hash,
store bytes, insert rows, queue the ingestion job. Actually running
ingestion/extraction is scheduled as a background task by the route handler,
exactly like `ConversationService`/`app/api/routes/conversations.py` already
does for message extraction (docs/adr/0008's pattern, reused unchanged for
documents rather than reinvented).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.documents.parsers.base import DocumentParseError
from app.documents.validation import validate_upload
from app.models.document import Document, DocumentChunk
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.ingestion_repository import IngestionJobRepository
from app.repositories.user_repository import UserRepository
from app.storage.base import DocumentStorage


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DocumentService:
    def __init__(
        self,
        session: AsyncSession,
        storage: DocumentStorage,
        *,
        settings: Settings | None = None,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._session = session
        self._storage = storage
        self._settings = settings or get_settings()
        self._now = now_fn
        self._new_id = id_fn

    async def upload_document(
        self, *, user_id: str, filename: str, content: bytes
    ) -> tuple[Document, bool]:
        """Validate, store, and queue one uploaded file for ingestion.

        Returns `(document, is_new)`. `is_new=False` means this exact file
        (by content hash) was already uploaded by this user -- the existing
        `Document` is returned rather than a duplicate
        (`DocumentRepository.create`'s `ON CONFLICT` dedup, section 8), and
        no second ingestion job is queued for it.

        Raises `ValidationError` (422) for anything `validate_upload` rejects
        -- empty file, oversized file, or a content type v1 does not support
        -- so a bad upload fails synchronously with an actionable message,
        rather than being accepted and only failing later inside the
        background ingestion pipeline.
        """
        now = self._now()
        try:
            validation = validate_upload(
                content,
                filename=filename,
                max_size_bytes=self._settings.max_document_size_bytes,
            )
        except DocumentParseError as exc:
            raise ValidationError(exc.message, details={"error_code": exc.error_code}) from exc
        content_hash = hashlib.sha256(content).hexdigest()

        await UserRepository(self._session).ensure_exists(user_id, now=now)
        put_result = await self._storage.put(data=content, user_id=user_id)
        document, is_new = await DocumentRepository(self._session, user_id=user_id).create(
            document_id=self._new_id(),
            filename=validation.sanitized_filename,
            content_type=validation.content_type,
            size_bytes=len(content),
            content_hash=content_hash,
            storage_key=put_result.storage_key,
            now=now,
        )
        if not is_new:
            # This exact file was already uploaded -- the bytes just written
            # are an orphaned duplicate of the existing document's own copy;
            # clean them up rather than leaving unreferenced disk usage.
            await self._storage.delete(storage_key=put_result.storage_key, user_id=user_id)
            return document, False

        await IngestionJobRepository(self._session, user_id=user_id).create_job(
            job_id=self._new_id(), document_id=document.id, now=now
        )
        return document, True

    async def get_document(self, *, user_id: str, document_id: str) -> Document:
        document = await DocumentRepository(self._session, user_id=user_id).get(
            document_id=document_id
        )
        if document is None:
            raise NotFoundError(f"No document found with id {document_id!r}")
        return document

    async def list_documents(self, *, user_id: str, limit: int, offset: int) -> list[Document]:
        return await DocumentRepository(self._session, user_id=user_id).list(
            limit=limit, offset=offset
        )

    async def list_chunks(self, *, user_id: str, document_id: str) -> list[DocumentChunk]:
        """Raises `NotFoundError` if the document itself does not exist for
        this user -- an empty chunk list for an existing-but-not-yet-chunked
        document is a normal, distinct outcome from "no such document".
        """
        await self.get_document(user_id=user_id, document_id=document_id)
        return await DocumentChunkRepository(self._session, user_id=user_id).list_for_document(
            document_id=document_id
        )
