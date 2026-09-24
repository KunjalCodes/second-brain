"""Conversation and message business logic.

Conversation and message CRUD, plus the *synchronous* half of extraction
triggering: the deterministic gate and job debounce
(`app.extraction.trigger.maybe_trigger_extraction`) run inside the same
transaction as storing the message, because both are cheap, pure bookkeeping with
no LLM call. The *asynchronous* half -- actually calling the model -- is scheduled
as a FastAPI background task by the route handler after the response is returned
(docs/adr/0008); this service never calls an LLM.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.extraction.trigger import maybe_trigger_extraction
from app.models.conversation import Conversation, ConversationDetail, Message, MessageRole
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.extraction_repository import ExtractionJobRepository
from app.repositories.message_repository import ConversationNotFound, MessageRepository
from app.repositories.user_repository import UserRepository
from app.schemas.conversation import ConversationListResponse, MessageListResponse


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ConversationService:
    """Owns conversation and message persistence, and user isolation between them."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._now = now_fn
        self._new_id = id_fn

    # -- commands ------------------------------------------------------------

    async def create_conversation(
        self, *, user_id: str, title: str | None
    ) -> Conversation:
        now = self._now()
        # Every user-owned table has a foreign key to users.id, so the identity
        # reference must exist before the first conversation. Idempotent: a no-op
        # for every subsequent call from the same user.
        await UserRepository(self._session).ensure_exists(user_id, now=now)

        repo = ConversationRepository(self._session, user_id=user_id)
        return await repo.create(
            conversation_id=self._new_id(), title=title, now=now
        )

    async def append_message(
        self,
        *,
        user_id: str,
        conversation_id: str,
        role: MessageRole,
        content: str,
    ) -> Message:
        """Store one message, then run the synchronous half of extraction triggering.

        The message write, the gate, and job creation/extension all happen in this
        one transaction -- none of it calls an LLM, so none of it adds request
        latency. The route handler checks the returned message's
        `extraction_status`: `queued` means it should schedule the actual (async,
        post-response) extraction work; `skipped` means the gate declined and there
        is nothing to schedule yet.
        """
        message_repo = MessageRepository(self._session, user_id=user_id)
        try:
            message = await message_repo.append(
                message_id=self._new_id(),
                conversation_id=conversation_id,
                role=role,
                content=content,
                token_count=None,  # Phase 3+: populate from the LLM provider
                now=self._now(),
            )
        except ConversationNotFound as exc:
            raise NotFoundError(
                "Conversation not found", details={"conversation_id": conversation_id}
            ) from exc

        job_repo = ExtractionJobRepository(self._session, user_id=user_id)
        await maybe_trigger_extraction(
            message=message,
            message_repo=message_repo,
            job_repo=job_repo,
            settings=self._settings,
            now=self._now(),
            id_fn=self._new_id,
        )
        # Re-read: maybe_trigger_extraction updated extraction_status in place in
        # the database, but `message` above is the pre-update snapshot.
        refreshed = await message_repo.get(message_id=message.id)
        assert refreshed is not None  # just written, in the same transaction
        return refreshed

    # -- queries ---------------------------------------------------------------

    async def get_conversation(
        self, *, user_id: str, conversation_id: str, message_limit: int = 200
    ) -> ConversationDetail:
        conv_repo = ConversationRepository(self._session, user_id=user_id)
        conversation = await conv_repo.get(conversation_id=conversation_id)
        if conversation is None:
            raise NotFoundError(
                "Conversation not found", details={"conversation_id": conversation_id}
            )
        msg_repo = MessageRepository(self._session, user_id=user_id)
        messages = await msg_repo.list(
            conversation_id=conversation_id, limit=message_limit, offset=0
        )
        return ConversationDetail(conversation=conversation, messages=messages)

    async def list_conversations(
        self, *, user_id: str, limit: int, offset: int
    ) -> ConversationListResponse:
        repo = ConversationRepository(self._session, user_id=user_id)
        items = await repo.list(limit=limit, offset=offset)
        total = await repo.count()
        return ConversationListResponse(
            items=items, total=total, limit=limit, offset=offset
        )

    async def list_messages(
        self, *, user_id: str, conversation_id: str, limit: int, offset: int
    ) -> MessageListResponse:
        # Ownership check: a conversation belonging to another user must read as
        # not-found here too, not merely on the conversation-detail endpoint.
        conv_repo = ConversationRepository(self._session, user_id=user_id)
        if await conv_repo.get(conversation_id=conversation_id) is None:
            raise NotFoundError(
                "Conversation not found", details={"conversation_id": conversation_id}
            )
        msg_repo = MessageRepository(self._session, user_id=user_id)
        items = await msg_repo.list(
            conversation_id=conversation_id, limit=limit, offset=offset
        )
        return MessageListResponse(items=items, limit=limit, offset=offset)
