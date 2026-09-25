"""FastAPI dependencies.

The identity dependency is the single place the request's user id is determined.
Replacing the Phase 1 header placeholder with real authentication in Phase 9 means
changing `get_current_user_id` and nothing else.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import ToolDependencies
from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError, UnauthorizedError, ValidationError
from app.db.engine import Database, get_database
from app.embeddings.base import EmbeddingClient
from app.embeddings.factory import get_embedding_client
from app.graph.connection import GraphClient, get_graph_client
from app.llm.base import LLMClient
from app.llm.factory import get_llm_client
from app.repositories.entity_repository import EntityRepository
from app.repositories.graph_repository import GraphRepository
from app.repositories.memory_repository import MemoryRepository
from app.services.agent_service import AgentService
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.services.extraction_status_service import ExtractionStatusService
from app.services.graph_exploration_service import GraphExplorationService
from app.services.hybrid_retrieval_service import HybridRetrievalService
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService
from app.services.review_action_service import ReviewActionService
from app.services.review_service import ReviewService
from app.storage.base import DocumentStorage
from app.storage.factory import get_document_storage

#: Maximum accepted length of a user id, to keep an absurd header from reaching Cypher.
MAX_USER_ID_LENGTH = 128


def get_app_settings() -> Settings:
    return get_settings()


def get_client() -> GraphClient:
    return get_graph_client()


def get_db() -> Database:
    return get_database()


def get_llm() -> LLMClient:
    return get_llm_client()


def get_embeddings() -> EmbeddingClient:
    return get_embedding_client()


def get_storage() -> DocumentStorage:
    return get_document_storage()


async def get_db_session(
    db: Annotated[Database, Depends(get_db)],
) -> AsyncIterator[AsyncSession]:
    """One session per request: commits if the request succeeds, rolls back if it
    raises. See `Database.session` for the commit/rollback contract.
    """
    async with db.session() as session:
        yield session


def get_current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    settings: Annotated[Settings, Depends(get_app_settings)] = ...,  # type: ignore[assignment]
) -> str:
    """Resolve the requesting user.

    **This is not authentication.** Phase 1 has no auth (it is Phase 9), so the user
    id is taken from the `X-User-Id` header and falls back to `DEV_USER_ID`. It exists
    so that every record is namespaced from the first commit and so the isolation
    tests are meaningful; it must not be exposed to untrusted callers. `APP_ENV=production`
    together with `ALLOW_USER_ID_HEADER=true` is rejected at startup.
    """
    if not settings.allow_user_id_header:
        raise ConfigurationError(
            "Authentication is not implemented yet (Phase 9). Set "
            "ALLOW_USER_ID_HEADER=true for development use."
        )
    user_id = (x_user_id or settings.dev_user_id).strip()
    if not user_id:
        raise ValidationError("X-User-Id must not be empty")
    if len(user_id) > MAX_USER_ID_LENGTH:
        raise ValidationError(f"X-User-Id must be at most {MAX_USER_ID_LENGTH} characters")
    return user_id


def get_memory_service(
    client: Annotated[GraphClient, Depends(get_client)],
) -> MemoryService:
    return MemoryService(MemoryRepository(client))


def get_retrieval_service(
    client: Annotated[GraphClient, Depends(get_client)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> RetrievalService:
    return RetrievalService(
        EntityRepository(client),
        max_hops=settings.graph_max_hops,
        max_nodes=settings.graph_max_nodes,
        max_edges=settings.graph_max_edges,
    )


def get_conversation_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ConversationService:
    return ConversationService(session, settings=settings)


def get_document_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[DocumentStorage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DocumentService:
    return DocumentService(session, storage, settings=settings)


def get_provenance_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> ProvenanceService:
    return ProvenanceService(session, memory_service)


def get_graph_exploration_service(
    client: Annotated[GraphClient, Depends(get_client)],
    retrieval_service: Annotated[RetrievalService, Depends(get_retrieval_service)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    provenance_service: Annotated[ProvenanceService, Depends(get_provenance_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> GraphExplorationService:
    return GraphExplorationService(
        repository=GraphRepository(client),
        retrieval=retrieval_service,
        memory=memory_service,
        provenance=provenance_service,
        settings=settings,
    )


def get_hybrid_retrieval_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    retrieval_service: Annotated[RetrievalService, Depends(get_retrieval_service)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embeddings)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HybridRetrievalService:
    return HybridRetrievalService(
        session=session,
        memory_service=memory_service,
        retrieval_service=retrieval_service,
        embedding_client=embedding_client,
        settings=settings,
    )


def get_extraction_status_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ExtractionStatusService:
    return ExtractionStatusService(session)


def get_review_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    client: Annotated[GraphClient, Depends(get_client)],
) -> ReviewService:
    return ReviewService(
        session, memory_service=memory_service, entity_repository=EntityRepository(client)
    )


def get_review_action_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    review_service: Annotated[ReviewService, Depends(get_review_service)],
    client: Annotated[GraphClient, Depends(get_client)],
) -> ReviewActionService:
    return ReviewActionService(
        session,
        review_service=review_service,
        memory_repository=MemoryRepository(client),
        entity_repository=EntityRepository(client),
    )


def get_agent_service(
    llm_client: Annotated[LLMClient, Depends(get_llm)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AgentService:
    return AgentService(llm_client=llm_client, settings=settings)


def get_tool_dependencies(
    hybrid_retrieval_service: Annotated[
        HybridRetrievalService, Depends(get_hybrid_retrieval_service)
    ],
    retrieval_service: Annotated[RetrievalService, Depends(get_retrieval_service)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    provenance_service: Annotated[ProvenanceService, Depends(get_provenance_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    db: Annotated[Database, Depends(get_db)],
    graph_client: Annotated[GraphClient, Depends(get_client)],
    llm_client: Annotated[LLMClient, Depends(get_llm)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embeddings)],
) -> ToolDependencies:
    """Bundles the existing services one agent turn needs (PHASE5_PLAN.md
    section 3) -- the agent never constructs its own service or repository.

    `db`/`graph_client`/`llm_client`/`embedding_client` are only used by
    `propose_memory_update` (to build a real `ExtractionRunner`), but every
    read-only tool call carries them too since this is one dependency
    function for the whole turn -- they are inert until that one tool reads
    them. `conversation_id`/`target_message`/`window_text` are NOT set here:
    they are this turn's own request context, not a plain dependency
    injection value, so the route handler attaches them via
    `dataclasses.replace` before calling `AgentService.run_turn`.
    """
    return ToolDependencies(
        hybrid_retrieval=hybrid_retrieval_service,
        retrieval=retrieval_service,
        memory=memory_service,
        provenance=provenance_service,
        settings=settings,
        db=db,
        graph_client=graph_client,
        llm_client=llm_client,
        embedding_client=embedding_client,
    )


def require_internal_token(
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
    settings: Annotated[Settings, Depends(get_app_settings)] = ...,  # type: ignore[assignment]
) -> None:
    """Guard for internal-only operational endpoints (currently: the sweep trigger).

    Not user-scoped and not part of the public API surface -- a mismatched or
    absent token is always rejected, including when `INTERNAL_API_TOKEN` itself is
    unset, so the endpoint fails closed rather than open.
    """
    if not settings.internal_api_token:
        raise ConfigurationError(
            "INTERNAL_API_TOKEN is not set. This endpoint is disabled until it is."
        )
    # Constant-time comparison: this guards an operational endpoint, not a
    # customer-facing one, but a plain `!=` still leaks the matching prefix length
    # through response timing, and there is no cost to closing that off.
    if not hmac.compare_digest(x_internal_token or "", settings.internal_api_token):
        raise UnauthorizedError("Missing or invalid X-Internal-Token header")


CurrentUser = Annotated[str, Depends(get_current_user_id)]
MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
DocumentStorageDep = Annotated[DocumentStorage, Depends(get_storage)]
ProvenanceServiceDep = Annotated[ProvenanceService, Depends(get_provenance_service)]
HybridRetrievalServiceDep = Annotated[HybridRetrievalService, Depends(get_hybrid_retrieval_service)]
ExtractionStatusServiceDep = Annotated[
    ExtractionStatusService, Depends(get_extraction_status_service)
]
ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]
ReviewActionServiceDep = Annotated[ReviewActionService, Depends(get_review_action_service)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
ToolDependenciesDep = Annotated[ToolDependencies, Depends(get_tool_dependencies)]
GraphExplorationServiceDep = Annotated[
    GraphExplorationService, Depends(get_graph_exploration_service)
]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
LLMClientDep = Annotated[LLMClient, Depends(get_llm)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embeddings)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
InternalTokenDep = Annotated[None, Depends(require_internal_token)]
