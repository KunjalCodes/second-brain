"""Memory endpoints.

Handlers do no business logic: they bind HTTP to the service layer and nothing else.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status

from app.api.deps import (
    CurrentUser,
    EmbeddingClientDep,
    MemoryServiceDep,
    ProvenanceServiceDep,
    SettingsDep,
)
from app.core.config import Settings
from app.db.engine import get_database
from app.embeddings.base import EmbeddingClient
from app.models.domain import Memory, MemoryDetail
from app.models.enums import MemoryStatus, MemoryType
from app.schemas.memory import (
    MemoryCreate,
    MemoryInvalidate,
    MemoryListResponse,
    MemoryUpdate,
)
from app.schemas.provenance import ProvenanceView
from app.semantic.embedding_pipeline import embed_and_store_memory

router = APIRouter(prefix="/memories", tags=["memories"])
logger = logging.getLogger(__name__)


async def _background_embed(
    *, user_id: str, memory_id: str, content: str, embedding_client: EmbeddingClient,
    settings: Settings,
) -> None:
    """Runs after the HTTP response has been sent (ADR 0009, PHASE3_PLAN.md
    section 3): embedding is an asynchronous enhancement, never a precondition
    for a memory existing. Never raises into the request/response cycle --
    `embed_and_store_memory` itself never raises either, but this mirrors
    `app.api.routes.conversations._background_extraction_tick`'s belt-and-braces
    pattern in case a future change to that function ever did.
    """
    try:
        await embed_and_store_memory(
            user_id=user_id, memory_id=memory_id, content=content,
            db=get_database(), embedding_client=embedding_client, settings=settings,
        )
    except Exception:  # noqa: BLE001 - background task; must not raise further
        logger.exception("Background embedding failed for memory %s", memory_id)


@router.post(
    "",
    response_model=MemoryDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a memory",
)
async def create_memory(
    payload: MemoryCreate,
    user_id: CurrentUser,
    service: MemoryServiceDep,
    background_tasks: BackgroundTasks,
    embedding_client: EmbeddingClientDep,
    settings: SettingsDep,
) -> MemoryDetail:
    """Store a memory with its entities, relationships, provenance and supersessions.

    Listing ids in `supersedes` closes those memories' validity windows and marks them
    `superseded`; it never deletes them. Embedding is scheduled as a background task
    (ADR 0009) so a slow or unavailable embedding provider never delays this response.
    """
    created = await service.create(user_id=user_id, payload=payload)
    background_tasks.add_task(
        _background_embed,
        user_id=user_id,
        memory_id=created.memory.id,
        content=created.memory.content,
        embedding_client=embedding_client,
        settings=settings,
    )
    return created


# Declared before /{memory_id} so that "search" is not captured as an id.
@router.get("/search", response_model=MemoryListResponse, summary="Search memories")
async def search_memories(
    user_id: CurrentUser,
    service: MemoryServiceDep,
    settings: SettingsDep,
    q: Annotated[
        str | None, Query(description="Case-insensitive substring of content")
    ] = None,
    status_filter: Annotated[
        list[MemoryStatus] | None,
        Query(
            alias="status",
            description="Repeatable. Defaults to all but invalid/archived.",
        ),
    ] = None,
    memory_type: Annotated[list[MemoryType] | None, Query()] = None,
    entity: Annotated[
        str | None, Query(description="Only memories mentioning this entity name")
    ] = None,
    as_of: Annotated[
        datetime | None,
        Query(description="Only memories whose validity window contains this instant"),
    ] = None,
    include_excluded: Annotated[
        bool, Query(description="Include invalid and archived memories")
    ] = False,
    limit: Annotated[int, Query(ge=1)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryListResponse:
    return await service.search(
        user_id=user_id,
        query=q,
        statuses=status_filter,
        memory_types=memory_type,
        entity_name=entity,
        as_of=as_of,
        include_excluded=include_excluded,
        limit=min(limit, settings.search_max_limit),
        offset=offset,
    )


@router.get("/{memory_id}", response_model=MemoryDetail, summary="Get one memory")
async def get_memory(
    memory_id: str,
    user_id: CurrentUser,
    service: MemoryServiceDep,
) -> MemoryDetail:
    return await service.get(user_id=user_id, memory_id=memory_id)


@router.get(
    "/{memory_id}/provenance",
    response_model=ProvenanceView,
    summary="Why does the system believe this memory?",
)
async def get_memory_provenance(
    memory_id: str,
    user_id: CurrentUser,
    service: ProvenanceServiceDep,
) -> ProvenanceView:
    """Resolve a memory's sources back to the messages that produced them.

    For Phase 1 (manually created) memories this returns the source metadata with
    no message attached. For Phase 2 extracted memories it resolves each source
    back through PostgreSQL to the actual conversation message.
    """
    return await service.get_provenance(user_id=user_id, memory_id=memory_id)


@router.patch("/{memory_id}", response_model=Memory, summary="Update memory metadata")
async def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    user_id: CurrentUser,
    service: MemoryServiceDep,
) -> Memory:
    """Update metadata only.

    `content` cannot be changed: rewriting stored text in place would destroy the
    historical record. Express a changed fact by creating a new memory whose
    `supersedes` names this one.
    """
    return await service.update(user_id=user_id, memory_id=memory_id, payload=payload)


@router.post(
    "/{memory_id}/invalidate",
    response_model=Memory,
    summary="Retract a memory",
)
async def invalidate_memory(
    memory_id: str,
    payload: MemoryInvalidate,
    user_id: CurrentUser,
    service: MemoryServiceDep,
) -> Memory:
    """Mark a memory invalid and close the relationships it asserted.

    There is deliberately no `DELETE`: memory is append-only, so retraction is
    recorded rather than erased. A `DELETE` verb would imply the data is gone.
    """
    return await service.invalidate(
        user_id=user_id, memory_id=memory_id, reason=payload.reason
    )
