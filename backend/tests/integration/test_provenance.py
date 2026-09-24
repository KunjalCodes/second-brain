"""Memory -> Source -> Message -> Conversation resolution (docs/PHASE2_PLAN.md
section 2), the one place this codebase deliberately crosses the PostgreSQL/Neo4j
boundary in a read.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models import DocumentChunk as ChunkRow
from app.models.conversation import ExtractionStatus, MessageRole
from app.models.document import format_chunk_source_location
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService
from tests.conftest import JAN

pytestmark = pytest.mark.integration


@pytest.fixture
def memory_service(graph) -> MemoryService:  # noqa: ANN001
    return MemoryService(MemoryRepository(graph))


@pytest.fixture
def retrieval(graph) -> RetrievalService:  # noqa: ANN001
    return RetrievalService(EntityRepository(graph), max_hops=4)


class TestProvenance:
    async def test_resolves_a_message_backed_source_to_the_real_message(
        self,
        db,
        graph,
        memory_service,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            conv = await ConversationRepository(session, user_id=user_id).create(
                conversation_id=str(uuid.uuid4()), title="t", now=JAN
            )
            message = await MessageRepository(session, user_id=user_id).append(
                message_id=str(uuid.uuid4()),
                conversation_id=conv.id,
                role=MessageRole.USER,
                content="The project uses Gemini for classification.",
                token_count=None,
                now=JAN,
            )

        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses Gemini",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "Gemini", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "Gemini"}
                    ],
                    "source": {
                        "source_type": "message",
                        "reference": message.id,
                        "location": "0:30",
                    },
                }
            ),
        )

        async with db.session() as session:
            provenance_service = ProvenanceService(session, memory_service)
            view = await provenance_service.get_provenance(
                user_id=user_id, memory_id=created.memory.id
            )

        assert len(view.sources) == 1
        assert view.sources[0].source.reference == message.id
        assert view.sources[0].message is not None
        assert view.sources[0].message.content == "The project uses Gemini for classification."
        assert view.sources[0].message.conversation_id == conv.id

    async def test_non_message_source_resolves_with_no_message(
        self,
        db,
        graph,
        memory_service,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """Phase 1 (manually created) memories may cite a conversation-level
        source with no specific message id -- provenance must not error on that.
        """
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)

        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses EasyOCR",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "EasyOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "EasyOCR"}
                    ],
                    "source": {"source_type": "conversation", "reference": "conversation-101"},
                }
            ),
        )

        async with db.session() as session:
            provenance_service = ProvenanceService(session, memory_service)
            view = await provenance_service.get_provenance(
                user_id=user_id, memory_id=created.memory.id
            )

        assert len(view.sources) == 1
        assert view.sources[0].message is None
        assert view.sources[0].source.reference == "conversation-101"

    async def test_multiple_sources_from_duplicate_corroboration_all_resolve(
        self,
        db,
        graph,
        memory_service,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            conv = await ConversationRepository(session, user_id=user_id).create(
                conversation_id=str(uuid.uuid4()), title="t", now=JAN
            )
            msg_repo = MessageRepository(session, user_id=user_id)
            first_message = await msg_repo.append(
                message_id=str(uuid.uuid4()),
                conversation_id=conv.id,
                role=MessageRole.USER,
                content="We use Gemini for classification.",
                token_count=None,
                now=JAN,
            )
            second_message = await msg_repo.append(
                message_id=str(uuid.uuid4()),
                conversation_id=conv.id,
                role=MessageRole.USER,
                content="Just confirming: we use Gemini.",
                token_count=None,
                now=JAN,
            )

        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses Gemini",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "Gemini", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "Gemini"}
                    ],
                    "source": {
                        "source_type": "message",
                        "reference": first_message.id,
                        "location": "0:10",
                    },
                }
            ),
        )
        from app.models.enums import SourceType

        await memory_service.attach_source(
            user_id=user_id,
            memory_id=created.memory.id,
            source_type=SourceType.MESSAGE,
            reference=second_message.id,
            location="0:10",
        )

        async with db.session() as session:
            provenance_service = ProvenanceService(session, memory_service)
            view = await provenance_service.get_provenance(
                user_id=user_id, memory_id=created.memory.id
            )

        assert len(view.sources) == 2
        resolved_messages = {s.message.content for s in view.sources if s.message}
        assert resolved_messages == {
            "We use Gemini for classification.",
            "Just confirming: we use Gemini.",
        }

    async def test_missing_memory_raises_not_found(
        self,
        db,
        graph,
        memory_service,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        from app.core.errors import NotFoundError

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            provenance_service = ProvenanceService(session, memory_service)
            with pytest.raises(NotFoundError):
                await provenance_service.get_provenance(
                    user_id=user_id, memory_id="00000000-0000-0000-0000-000000000000"
                )

    async def test_cross_user_provenance_is_not_found(
        self,
        db,
        graph,
        memory_service,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        from app.core.errors import NotFoundError

        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)

        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Private fact",
                    "entities": [{"name": "Solo Entity", "entity_type": "concept"}],
                }
            ),
        )

        async with db.session() as session:
            provenance_service = ProvenanceService(session, memory_service)
            with pytest.raises(NotFoundError):
                await provenance_service.get_provenance(
                    user_id=other_user_id, memory_id=created.memory.id
                )

    async def test_message_lookup_itself_is_user_scoped_defense_in_depth(
        self,
        db,
        graph,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """The test above proves the end-to-end path is safe because
        `MemoryService.get` rejects a cross-user memory before any message lookup
        happens. This test isolates the *second* gate on its own: even given
        another user's real message id directly, `MessageRepository.get` -- what
        `ProvenanceService` actually calls -- must not return it for a different
        user. This is what stops a future provenance-adjacent code path that skips
        the memory-ownership check from leaking a message across users
        (CLAUDE.md section 26/28, docs/PHASE2_HARDENING.md section 8).
        """
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            conv = await ConversationRepository(session, user_id=user_id).create(
                conversation_id=str(uuid.uuid4()), title="t", now=JAN
            )
            owned_message = await MessageRepository(session, user_id=user_id).append(
                message_id=str(uuid.uuid4()),
                conversation_id=conv.id,
                role=MessageRole.USER,
                content="User A's private message.",
                token_count=None,
                now=JAN,
            )

        async with db.session() as session:
            as_other_user = MessageRepository(session, user_id=other_user_id)
            leaked = await as_other_user.get(message_id=owned_message.id)

        assert leaked is None, "a message must never resolve under a different user's scope"


class TestDocumentProvenance:
    """Phase 6 (ADR 0012): a DOCUMENT-sourced memory resolves both the parent
    `Document` (from `Source.reference`) and the specific `DocumentChunk`
    (from the chunk id embedded in `Source.location`).
    """

    async def _make_document_and_chunk(self, db, *, user_id: str):  # noqa: ANN001, ANN202
        async with db.session() as session:
            document, _ = await DocumentRepository(session, user_id=user_id).create(
                document_id=str(uuid.uuid4()),
                filename="report.txt",
                content_type="text/plain",
                size_bytes=100,
                content_hash=str(uuid.uuid4()),
                storage_key=str(uuid.uuid4()),
                now=JAN,
            )
            chunk_row = ChunkRow(
                id=str(uuid.uuid4()),
                document_id=document.id,
                user_id=user_id,
                ordinal=0,
                content="Drawing Extractor uses PaddleOCR for text recognition.",
                content_hash="deadbeef",
                page_start=1,
                page_end=1,
                heading_context="Overview",
                char_start=0,
                char_end=56,
                extraction_status=ExtractionStatus.DONE.value,
                created_at=JAN,
            )
            await DocumentChunkRepository(session, user_id=user_id).bulk_create(chunks=[chunk_row])
        return document.id, chunk_row.id

    async def test_resolves_a_document_backed_source_to_document_and_chunk(
        self,
        db,
        graph,
        memory_service,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        document_id, chunk_id = await self._make_document_and_chunk(db, user_id=user_id)

        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses PaddleOCR",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "PaddleOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "PaddleOCR"}
                    ],
                    "source": {
                        "source_type": "document",
                        "reference": document_id,
                        "location": f"{format_chunk_source_location(chunk_id)}:0:56",
                    },
                }
            ),
        )

        async with db.session() as session:
            provenance_service = ProvenanceService(session, memory_service)
            view = await provenance_service.get_provenance(
                user_id=user_id, memory_id=created.memory.id
            )

        assert len(view.sources) == 1
        resolved = view.sources[0]
        assert resolved.message is None
        assert resolved.document is not None
        assert resolved.document.id == document_id
        assert resolved.document.filename == "report.txt"
        assert resolved.document_chunk is not None
        assert resolved.document_chunk.id == chunk_id
        assert resolved.document_chunk.heading_context == "Overview"
        assert resolved.document_chunk.page_start == 1

    async def test_document_lookup_itself_is_user_scoped_defense_in_depth(
        self,
        db,
        graph,
        user_id,
        other_user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        """The counterpart to `test_message_lookup_itself_is_user_scoped_
        defense_in_depth`: another user's real document/chunk id must not
        resolve under a different user's scope.
        """
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
        document_id, chunk_id = await self._make_document_and_chunk(db, user_id=user_id)

        async with db.session() as session:
            leaked_document = await DocumentRepository(session, user_id=other_user_id).get(
                document_id=document_id
            )
            leaked_chunk = await DocumentChunkRepository(session, user_id=other_user_id).get(
                chunk_id=chunk_id
            )

        assert leaked_document is None, "a document must never resolve under a different user"
        assert leaked_chunk is None, "a chunk must never resolve under a different user"
