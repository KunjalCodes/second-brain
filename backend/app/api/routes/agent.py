"""The Phase 5 agent endpoint (docs/PHASE5_PLAN.md section 20 step 9).

Handlers do no agent logic themselves: this module binds HTTP to
`AgentService`/`ConversationService` and nothing else, matching
`app/api/routes/memories.py`'s and `conversations.py`'s own discipline. No
raw Cypher/SQL, no direct database access -- every read/write goes through
the same services the rest of the API already uses.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from fastapi import APIRouter, BackgroundTasks, status

from app.api.deps import (
    RATE_LIMIT_AGENT,
    AgentServiceDep,
    ConversationServiceDep,
    CurrentUser,
    DbSessionDep,
    EmbeddingClientDep,
    LLMClientDep,
    SettingsDep,
    ToolDependenciesDep,
)
from app.core.config import Settings
from app.db.engine import get_database
from app.embeddings.base import EmbeddingClient
from app.extraction.runner import run_worker_tick
from app.graph.connection import get_graph_client
from app.llm.base import LLMClient
from app.models.conversation import ExtractionStatus, MessageRole
from app.schemas.agent import AgentTurnRequest, AgentTurnResponse

router = APIRouter(prefix="/conversations", tags=["agent"])
logger = logging.getLogger(__name__)

#: Same bound `conversations.py` uses for the live post-message extraction
#: tick -- this is the live path firing once per turn, not the sweep.
_LIVE_TICK_LIMIT = 3

#: How many of the most recent messages the agent sees as its conversation
#: window -- mirrors `settings.extraction_window_size`'s own role (bounded
#: recent context, not the whole conversation), kept as its own read rather
#: than reusing that setting directly so the two can be tuned independently
#: without one phase's config silently changing another's behavior.
_AGENT_WINDOW_MESSAGES = 10


async def _background_extraction_tick(
    *, llm_client: LLMClient, embedding_client: EmbeddingClient, settings: Settings, worker_id: str
) -> None:
    """Identical in shape to `conversations.py`'s own background tick --
    duplicated rather than imported because that one is module-private
    (leading underscore) and this is a distinct call site, not a shared
    library function; the logic itself is a single `run_worker_tick` call.
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
            logger.info("Background extraction tick (agent turn) processed %d job(s)", processed)
    except Exception:  # noqa: BLE001 - background task; must not raise further
        logger.exception("Background extraction tick (agent turn) failed")


@router.post(
    "/{conversation_id}/agent-turn",
    dependencies=[RATE_LIMIT_AGENT],
    response_model=AgentTurnResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run one agent turn: store the user's message, reason over memory, answer",
)
async def run_agent_turn(
    conversation_id: str,
    payload: AgentTurnRequest,
    user_id: CurrentUser,
    conversation_service: ConversationServiceDep,
    agent_service: AgentServiceDep,
    tool_deps: ToolDependenciesDep,
    background_tasks: BackgroundTasks,
    llm_client: LLMClientDep,
    embedding_client: EmbeddingClientDep,
    settings: SettingsDep,
    db_session: DbSessionDep,
) -> AgentTurnResponse:
    """`user_id` comes from `CurrentUser` (the same authenticated-request
    dependency every other endpoint uses) and is threaded through as a plain
    Python parameter to `AgentService`/`ToolDependencies` -- it is never
    accepted from the request body, and no agent tool schema has a `user_id`
    field for a model to populate even if it tried (PHASE5_PLAN.md section
    19; statically checked by `test_agent_models.py`).

    The user's message is stored exactly like `POST /conversations/{id}/messages`
    would (same gate, same background extraction tick, same passive-extraction
    behavior) -- the agent turn is additive, never a replacement path that
    skips it. The assistant's final answer is stored the same way, under
    `role=ASSISTANT`; the extraction gate already rejects non-user messages
    (`app.extraction.gate.evaluate_gate`), so the agent's own text is never
    mistaken for a user-stated fact.
    """
    user_message = await conversation_service.append_message(
        user_id=user_id,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=payload.message,
    )
    if user_message.extraction_status is ExtractionStatus.QUEUED:
        background_tasks.add_task(
            _background_extraction_tick,
            llm_client=llm_client,
            embedding_client=embedding_client,
            settings=settings,
            worker_id=f"agent-live:{user_message.id}",
        )

    detail = await conversation_service.get_conversation(
        user_id=user_id, conversation_id=conversation_id, message_limit=1000
    )
    window = detail.messages[-_AGENT_WINDOW_MESSAGES:]
    window_text = "\n".join(f"{m.role.value}: {m.content}" for m in window)

    # `propose_memory_update` grounds its proposal against THIS turn's own
    # user message and window -- request-specific context a plain dependency
    # function cannot supply (see `get_tool_dependencies`'s docstring).
    turn_deps = replace(
        tool_deps,
        conversation_id=conversation_id,
        target_message=user_message,
        window_text=window_text,
    )
    result = await agent_service.run_turn(
        user_id=user_id,
        conversation_window_text=window_text,
        user_message=payload.message,
        deps=turn_deps,
    )

    assistant_message = await conversation_service.append_message(
        user_id=user_id,
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT,
        content=result.answer,
    )

    # Commit BEFORE returning (Phase 11, J10): FastAPI runs BackgroundTasks
    # before the request session's commit-on-exit, so without this the
    # extraction tick scheduled above could look for the user message's job
    # before it is committed and find nothing (the same race
    # `POST /conversations/{id}/messages` closes). Committing here -- after the
    # turn succeeded -- keeps the turn atomic: if `run_turn` raised, nothing
    # (not even the user message) was stored.
    await db_session.commit()
    return AgentTurnResponse(
        message=assistant_message,
        cited_memory_ids=result.cited_memory_ids,
        tool_call_count=result.tool_call_count,
        termination_reason=result.termination_reason,
        confidence=result.confidence,
    )
