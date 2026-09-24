"""Document and document-chunk persistence (Phase 6, PHASE6_PLAN.md section 3).

`UserScopedRepository` subclasses, exactly like every other PostgreSQL
repository -- covered by the existing SQL isolation audit
(`tests/unit/test_sql_safety.py`) with zero changes to that audit's scope
rules, since it already walks every file under `app/repositories/`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert

from app.db.models import Document as DocumentRow
from app.db.models import DocumentChunk as ChunkRow
from app.models.conversation import ExtractionStatus
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.repositories.sql_base import UserScopedRepository


def _document_to_domain(row: DocumentRow) -> Document:
    return Document(
        id=row.id,
        user_id=row.user_id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        content_hash=row.content_hash,
        storage_key=row.storage_key,
        page_count=row.page_count,
        status=DocumentStatus(row.status),
        error_code=row.error_code,
        error_detail=row.error_detail,
        uploaded_at=row.uploaded_at,
        processed_at=row.processed_at,
    )


def _chunk_to_domain(row: ChunkRow) -> DocumentChunk:
    return DocumentChunk(
        id=row.id,
        document_id=row.document_id,
        user_id=row.user_id,
        ordinal=row.ordinal,
        content=row.content,
        content_hash=row.content_hash,
        page_start=row.page_start,
        page_end=row.page_end,
        heading_context=row.heading_context,
        char_start=row.char_start,
        char_end=row.char_end,
        extraction_status=ExtractionStatus(row.extraction_status),
        created_at=row.created_at,
    )


class DocumentRepository(UserScopedRepository):
    async def create(
        self,
        *,
        document_id: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        content_hash: str,
        storage_key: str,
        now: datetime,
    ) -> tuple[Document, bool]:
        """Insert a document, or return the existing one for this user+hash.

        `ON CONFLICT (user_id, content_hash) DO NOTHING` is the whole-file
        dedup guard (PHASE6_PLAN.md section 8): re-uploading an identical
        file returns the existing row rather than creating a second one.
        Returns `(document, created)`.
        """
        stmt = (
            insert(DocumentRow)
            .values(
                id=document_id,
                user_id=self._user_id,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                content_hash=content_hash,
                storage_key=storage_key,
                status=DocumentStatus.UPLOADED.value,
                uploaded_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "content_hash"])
            .returning(DocumentRow)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return _document_to_domain(row), True

        stmt = self._select(DocumentRow).where(DocumentRow.content_hash == content_hash)
        result = await self._session.execute(stmt)
        return _document_to_domain(result.scalar_one()), False

    async def get(self, *, document_id: str) -> Document | None:
        row = await self._session.get(DocumentRow, document_id)
        if row is None or row.user_id != self._user_id:
            return None
        return _document_to_domain(row)

    async def list(self, *, limit: int, offset: int) -> list[Document]:
        stmt = (
            self._select(DocumentRow)
            .order_by(DocumentRow.uploaded_at.desc(), DocumentRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [_document_to_domain(row) for row in result.scalars()]

    async def set_status(
        self,
        *,
        document_id: str,
        status: DocumentStatus,
        error_code: str | None = None,
        error_detail: str | None = None,
        page_count: int | None = None,
        processed_at: datetime | None = None,
    ) -> None:
        values: dict[str, object] = {"status": status.value}
        # `error_code`/`error_detail` are only ever cleared by moving past
        # FAILED, never overwritten with None by a normal progress update --
        # callers pass them explicitly only when setting or clearing a
        # failure, so a plain progress transition never wipes a prior error.
        if error_code is not None or status is not DocumentStatus.FAILED:
            values["error_code"] = error_code
            values["error_detail"] = error_detail
        if page_count is not None:
            values["page_count"] = page_count
        if processed_at is not None:
            values["processed_at"] = processed_at
        stmt = self._update(DocumentRow).where(DocumentRow.id == document_id).values(**values)
        await self._session.execute(stmt)


class DocumentChunkRepository(UserScopedRepository):
    async def bulk_create(self, *, chunks: list[ChunkRow]) -> None:
        """Insert every chunk for one document in one statement.

        Callers build `ChunkRow` instances directly (mirrors no existing
        precedent needed here -- there is no idempotency concern at this
        layer: chunking only ever runs once per `IngestionJob` attempt, and
        a whole failed ingestion attempt is retried from PARSING, which
        first clears any partial chunk rows via `delete_all_for_document`).
        """
        self._session.add_all(chunks)
        await self._session.flush()

    async def delete_all_for_document(self, *, document_id: str) -> None:
        stmt = self._delete(ChunkRow).where(ChunkRow.document_id == document_id)
        await self._session.execute(stmt)

    async def get(self, *, chunk_id: str) -> DocumentChunk | None:
        row = await self._session.get(ChunkRow, chunk_id)
        if row is None or row.user_id != self._user_id:
            return None
        return _chunk_to_domain(row)

    async def list_for_document(self, *, document_id: str) -> list[DocumentChunk]:
        stmt = (
            self._select(ChunkRow)
            .where(ChunkRow.document_id == document_id)
            .order_by(ChunkRow.ordinal)
        )
        result = await self._session.execute(stmt)
        return [_chunk_to_domain(row) for row in result.scalars()]

    async def list_by_ordinal_range(
        self, *, document_id: str, start_ordinal: int, end_ordinal: int
    ) -> list[DocumentChunk]:
        """The window a document extraction job reads -- inclusive on both
        ends, the exact counterpart to
        `MessageRepository.list_by_sequence_range`.
        """
        stmt = (
            self._select(ChunkRow)
            .where(
                ChunkRow.document_id == document_id,
                ChunkRow.ordinal >= start_ordinal,
                ChunkRow.ordinal <= end_ordinal,
            )
            .order_by(ChunkRow.ordinal)
        )
        result = await self._session.execute(stmt)
        return [_chunk_to_domain(row) for row in result.scalars()]

    async def set_extraction_status(self, *, chunk_id: str, status: ExtractionStatus) -> None:
        stmt = (
            self._update(ChunkRow)
            .where(ChunkRow.id == chunk_id)
            .values(extraction_status=status.value)
        )
        await self._session.execute(stmt)

    async def count_by_extraction_status(self, *, document_id: str) -> dict[str, int]:
        """`{status: count}` for one document's chunks -- what
        `DocumentService` uses to roll a document's coarse status up from
        its chunks' fine-grained extraction progress (PHASE6_PLAN.md
        section 2).
        """
        chunks = await self.list_for_document(document_id=document_id)
        counts: dict[str, int] = {}
        for chunk in chunks:
            counts[chunk.extraction_status.value] = counts.get(chunk.extraction_status.value, 0) + 1
        return counts
