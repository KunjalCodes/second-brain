"""Conversation and message persistence against real PostgreSQL.

Covers what unit tests cannot: real foreign keys, the unique
`(conversation_id, sequence)` constraint, and the row lock that serializes
sequence assignment under concurrency.

Every logical operation opens its own short-lived, auto-committing session via
`db.session()` -- one `ConversationService` per "request", exactly mirroring how
`app.api.deps.get_conversation_service` constructs one per real HTTP request.
Holding a single session open across a whole test (via the `db_session` fixture)
does not fit this file: `pg_cleanup` runs in its own session and cannot see, let
alone delete, rows a held-open session has not committed yet, and the two fixtures'
teardown order relative to each other is not something to depend on.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models.conversation import ExtractionStatus, MessageRole
from app.services.conversation_service import ConversationService
from tests.conftest import JAN

pytestmark = pytest.mark.integration

_NO_EXTRACTION = Settings(extraction_enabled=False)  # isolates CRUD tests from the gate


async def create_conversation(db, *, user_id: str, title: str | None, settings=_NO_EXTRACTION):  # noqa: ANN001
    async with db.session() as session:
        service = ConversationService(session, settings=settings, now_fn=lambda: JAN)
        return await service.create_conversation(user_id=user_id, title=title)


async def append_message(
    db, *, user_id: str, conversation_id: str, content: str,  # noqa: ANN001
    role: MessageRole = MessageRole.USER, settings=_NO_EXTRACTION, now=JAN,
):
    async with db.session() as session:
        service = ConversationService(session, settings=settings, now_fn=lambda: now)
        return await service.append_message(
            user_id=user_id, conversation_id=conversation_id, role=role, content=content
        )


class TestCreateConversation:
    async def test_creates_and_persists(self, db, user_id: str, pg_cleanup: None) -> None:  # noqa: ANN001
        conv = await create_conversation(db, user_id=user_id, title="My chat")
        assert conv.user_id == user_id
        assert conv.title == "My chat"
        assert conv.created_at == JAN

    async def test_title_is_optional(self, db, user_id: str, pg_cleanup: None) -> None:  # noqa: ANN001
        conv = await create_conversation(db, user_id=user_id, title=None)
        assert conv.title is None

    async def test_creates_the_identity_reference_idempotently(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """Two conversations for the same new user must not fail on the users FK."""
        await create_conversation(db, user_id=user_id, title="first")
        await create_conversation(db, user_id=user_id, title="second")
        async with db.session() as session:
            service = ConversationService(session, settings=_NO_EXTRACTION, now_fn=lambda: JAN)
            listing = await service.list_conversations(user_id=user_id, limit=10, offset=0)
        assert listing.total == 2


class TestAppendMessage:
    async def test_stores_the_message(self, db, user_id: str, pg_cleanup: None) -> None:  # noqa: ANN001
        conv = await create_conversation(db, user_id=user_id, title="t")
        message = await append_message(
            db, user_id=user_id, conversation_id=conv.id, content="hello"
        )
        assert message.content == "hello"
        assert message.conversation_id == conv.id
        assert message.sequence == 0

    async def test_sequence_is_monotonic_within_a_conversation(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv = await create_conversation(db, user_id=user_id, title="t")
        first = await append_message(db, user_id=user_id, conversation_id=conv.id, content="one")
        second = await append_message(db, user_id=user_id, conversation_id=conv.id, content="two")
        assert (first.sequence, second.sequence) == (0, 1)

    async def test_sequences_are_independent_per_conversation(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv_a = await create_conversation(db, user_id=user_id, title="a")
        conv_b = await create_conversation(db, user_id=user_id, title="b")
        msg_a = await append_message(db, user_id=user_id, conversation_id=conv_a.id, content="x")
        msg_b = await append_message(db, user_id=user_id, conversation_id=conv_b.id, content="y")
        assert msg_a.sequence == msg_b.sequence == 0

    async def test_appending_to_a_missing_conversation_raises_not_found(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        with pytest.raises(NotFoundError):
            await append_message(
                db, user_id=user_id,
                conversation_id="00000000-0000-0000-0000-000000000000",
                content="x",
            )

    async def test_concurrent_appends_to_the_same_conversation_get_distinct_sequences(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        """The row lock in ConversationRepository.get_for_update must serialize
        sequence assignment -- without it, two concurrent appends could both
        compute the same "next" sequence and violate the unique constraint (or
        worse, silently collide).
        """
        conv = await create_conversation(db, user_id=user_id, title="t")

        async def append_one(content: str) -> object:
            return await append_message(
                db, user_id=user_id, conversation_id=conv.id, content=content
            )

        results = await asyncio.gather(*(append_one(f"msg-{i}") for i in range(8)))
        sequences = sorted(m.sequence for m in results)
        assert sequences == list(range(8)), "sequence collision under concurrency"


class TestExtractionStatusIsSetByTheGate:
    """The gate itself is unit-tested (test_extraction_gate.py); this confirms it
    is actually wired into the append path and its effect is persisted.
    """

    async def test_a_qualifying_message_is_queued(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        settings = Settings(extraction_enabled=True, extraction_min_message_chars=10)
        conv = await create_conversation(db, user_id=user_id, title="t", settings=settings)
        message = await append_message(
            db, user_id=user_id, conversation_id=conv.id,
            content="The project uses Gemini for classification.", settings=settings,
        )
        assert message.extraction_status is ExtractionStatus.QUEUED

    async def test_a_skipped_message_records_the_gate_reason(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        settings = Settings(extraction_enabled=True, extraction_min_message_chars=1000)
        conv = await create_conversation(db, user_id=user_id, title="t", settings=settings)
        message = await append_message(
            db, user_id=user_id, conversation_id=conv.id, content="short", settings=settings
        )
        assert message.extraction_status is ExtractionStatus.SKIPPED
        assert message.extraction_gate_reason is not None

    async def test_extraction_disabled_leaves_the_message_pending(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv = await create_conversation(db, user_id=user_id, title="t")
        message = await append_message(
            db, user_id=user_id, conversation_id=conv.id,
            content="The project uses Gemini for classification.",
        )
        assert message.extraction_status is ExtractionStatus.PENDING


class TestGetConversation:
    async def test_returns_conversation_with_messages_in_order(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv = await create_conversation(db, user_id=user_id, title="t")
        await append_message(db, user_id=user_id, conversation_id=conv.id, content="first")
        await append_message(db, user_id=user_id, conversation_id=conv.id, content="second")
        async with db.session() as session:
            service = ConversationService(session, settings=_NO_EXTRACTION, now_fn=lambda: JAN)
            detail = await service.get_conversation(user_id=user_id, conversation_id=conv.id)
        assert [m.content for m in detail.messages] == ["first", "second"]

    async def test_missing_conversation_raises_not_found(
        self, db, user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            service = ConversationService(session, settings=_NO_EXTRACTION, now_fn=lambda: JAN)
            with pytest.raises(NotFoundError):
                await service.get_conversation(
                    user_id=user_id, conversation_id="00000000-0000-0000-0000-000000000000"
                )


class TestIsolation:
    async def test_a_user_cannot_read_another_users_conversation(
        self, db, user_id: str, other_user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv = await create_conversation(db, user_id=user_id, title="private")
        async with db.session() as session:
            service = ConversationService(session, settings=_NO_EXTRACTION, now_fn=lambda: JAN)
            with pytest.raises(NotFoundError):
                await service.get_conversation(user_id=other_user_id, conversation_id=conv.id)

    async def test_a_user_cannot_append_to_another_users_conversation(
        self, db, user_id: str, other_user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv = await create_conversation(db, user_id=user_id, title="private")
        with pytest.raises(NotFoundError):
            await append_message(
                db, user_id=other_user_id, conversation_id=conv.id, content="intrusion attempt"
            )

    async def test_list_conversations_is_scoped_per_user(
        self, db, user_id: str, other_user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        await create_conversation(db, user_id=user_id, title="mine")
        async with db.session() as session:
            service = ConversationService(session, settings=_NO_EXTRACTION, now_fn=lambda: JAN)
            listing = await service.list_conversations(user_id=other_user_id, limit=10, offset=0)
        assert listing.total == 0

    async def test_list_messages_of_anothers_conversation_is_not_found(
        self, db, user_id: str, other_user_id: str, pg_cleanup: None  # noqa: ANN001
    ) -> None:
        conv = await create_conversation(db, user_id=user_id, title="private")
        async with db.session() as session:
            service = ConversationService(session, settings=_NO_EXTRACTION, now_fn=lambda: JAN)
            with pytest.raises(NotFoundError):
                await service.list_messages(
                    user_id=other_user_id, conversation_id=conv.id, limit=10, offset=0
                )
