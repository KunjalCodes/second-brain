"""Conversation and message endpoints.

Handlers do no business logic: they bind HTTP to the service layer and nothing
else, matching `app/api/routes/memories.py`. No LLM call happens in this module.
`append_message` schedules the actual extraction work as a FastAPI `BackgroundTask`
-- registered before the response is returned, but not executed until after it is
sent to the client -- so a slow or unavailable LLM provider never delays a chat
response (docs/adr/0008).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status

from app.api.deps import (
    ConversationServiceDep,
    CurrentUser,
    EmbeddingClientDep,
    LLMClientDep,
    SettingsDep,
)
from app.core.config import Settings
from app.db.engine import get_database
from app.embeddings.base import EmbeddingClient
from app.extraction.runner import run_worker_tick
from app.graph.connection import get_graph_client
from app.llm.base import LLMClient
from app.models.conversation import Conversation, ConversationDetail, ExtractionStatus, Message
from app.schemas.conversation import (
    ConversationCreate,
    ConversationListResponse,
    MessageCreate,
    MessageListResponse,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])
logger = logging.getLogger(__name__)

#: How many jobs one post-message background tick tries to drain. Small: this is
#: the *live* path firing on every qualifying message, not the sweep -- the sweep
#: (EXTRACTION_SWEEP_BATCH_SIZE) is what actually drains a large backlog.
_LIVE_TICK_LIMIT = 3


async def _background_extraction_tick(
    *,
    llm_client: LLMClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    worker_id: str,
) -> None:
    """Runs after the HTTP response has been sent (see `BackgroundTasks.add_task`
    below). Never raises into the request/response cycle -- any failure here was
    already recorded on the job itself by `ExtractionRunner.run`; an exception
    escaping this function would only be logged by the ASGI server, not surfaced
    to a client who already has their response.
    """
    try:
        processed = await run_worker_tick(
            db=get_database(),
            graph_client=get_graph_client(),
            llm_client=llm_client,
            embedding_client=embedding_client,
            settings=settings,
            worker_id=worker_id,
            limit=_LIVE_TICK_LIMIT,
        )
        if processed:
            logger.info("Background extraction tick processed %d job(s)", processed)
    except Exception:  # noqa: BLE001 - background task; must not raise further
        logger.exception("Background extraction tick failed")


@router.post(
    "",
    response_model=Conversation,
    status_code=status.HTTP_201_CREATED,
    summary="Create a conversation",
)
async def create_conversation(
    payload: ConversationCreate,
    user_id: CurrentUser,
    service: ConversationServiceDep,
) -> Conversation:
    return await service.create_conversation(user_id=user_id, title=payload.title)


@router.get("", response_model=ConversationListResponse, summary="List conversations")
async def list_conversations(
    user_id: CurrentUser,
    service: ConversationServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListResponse:
    return await service.list_conversations(
        user_id=user_id,
        limit=min(limit, settings.search_max_limit),
        offset=offset,
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    summary="Get a conversation with its messages",
)
async def get_conversation(
    conversation_id: str,
    user_id: CurrentUser,
    service: ConversationServiceDep,
) -> ConversationDetail:
    return await service.get_conversation(
        user_id=user_id, conversation_id=conversation_id
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
    summary="Append a message",
)
async def append_message(
    conversation_id: str,
    payload: MessageCreate,
    user_id: CurrentUser,
    service: ConversationServiceDep,
    background_tasks: BackgroundTasks,
    llm_client: LLMClientDep,
    embedding_client: EmbeddingClientDep,
    settings: SettingsDep,
) -> Message:
    """Store one message and, if it passed the extraction gate, schedule extraction.

    Storing the message -- including the deterministic gate and job debounce -- is
    synchronous and adds no LLM latency. The actual model call, when there is one,
    is scheduled as a background task that FastAPI runs only after this response
    has been sent, so a slow or unavailable LLM provider never delays a chat
    response (docs/adr/0008).
    """
    message = await service.append_message(
        user_id=user_id,
        conversation_id=conversation_id,
        role=payload.role,
        content=payload.content,
    )
    if message.extraction_status is ExtractionStatus.QUEUED:
        background_tasks.add_task(
            _background_extraction_tick,
            llm_client=llm_client,
            embedding_client=embedding_client,
            settings=settings,
            worker_id=f"live:{message.id}",
        )
    return message


@router.get(
    "/{conversation_id}/messages",
    response_model=MessageListResponse,
    summary="List messages in a conversation",
)
async def list_messages(
    conversation_id: str,
    user_id: CurrentUser,
    service: ConversationServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageListResponse:
    return await service.list_messages(
        user_id=user_id,
        conversation_id=conversation_id,
        limit=min(limit, settings.search_max_limit),
        offset=offset,
    )
