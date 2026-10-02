"""FastAPI dependencies.

The identity dependency is the single place the request's user id is determined.
Phase 9 replaced its input -- the client-asserted `X-User-Id` header -- with a
verified server-side session (`get_auth_context`); every route still consumes a
plain `user_id` through `CurrentUser` (docs/PHASE9_PLAN.md, ADR 0020).
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import ToolDependencies
from app.auth.http import UNSAFE_METHODS, extract_credential, origin_allowed
from app.auth.tokens import is_project_token
from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthenticationRequiredError,
    ConfigurationError,
    ForbiddenError,
    NotFoundError,
    OriginNotAllowedError,
    UnauthorizedError,
    ValidationError,
)
from app.core.logging import bind_user
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
from app.services.auth_service import Authenticated, AuthService
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.services.extraction_status_service import ExtractionStatusService
from app.services.git_ingest_service import GitIngestService
from app.services.graph_exploration_service import GraphExplorationService
from app.services.graph_intelligence_service import GraphIntelligenceService
from app.services.hybrid_retrieval_service import HybridRetrievalService
from app.services.memory_explanation_service import MemoryExplanationService
from app.services.memory_service import MemoryService
from app.services.project_event_service import ProjectEventService
from app.services.project_query_service import ProjectQueryService
from app.services.project_resume_service import ProjectResumeService
from app.services.project_service import ProjectScope, ProjectService
from app.services.provenance_service import ProvenanceService
from app.services.rate_limit_service import LimitClass, RateLimiter
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


@dataclass(frozen=True)
class AuthContext:
    """The request's resolved identity. `user_id` is ALWAYS server-derived:
    the `users.id` bound to a verified session, or -- only in the explicit
    development `dev_header` mode -- the placeholder header value."""

    user_id: str
    mode: Literal["session", "dev_header"]
    via: Literal["bearer", "cookie", "dev_header"]
    authenticated: Authenticated | None = None

    @property
    def session_id(self) -> str | None:
        return self.authenticated.session.id if self.authenticated else None


def get_clock() -> Callable[[], datetime]:
    """Overridable in tests to pin session-expiry boundaries exactly."""
    return lambda: datetime.now(UTC)


def get_auth_service(
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> AuthService:
    return AuthService(db, settings, now_fn=clock)


def get_rate_limiter(
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> RateLimiter:
    return RateLimiter(db, settings, now_fn=clock)


def _dev_header_identity(x_user_id: str | None, settings: Settings) -> str:
    user_id = (x_user_id or settings.dev_user_id).strip()
    if not user_id:
        raise ValidationError("X-User-Id must not be empty")
    if len(user_id) > MAX_USER_ID_LENGTH:
        raise ValidationError(f"X-User-Id must be at most {MAX_USER_ID_LENGTH} characters")
    return user_id


async def get_auth_context(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthContext:
    """Resolve the requesting user (docs/PHASE9_PLAN.md section 6).

    1. A presented credential (`Authorization: Bearer`, else the session
       cookie) is always verified -- in either mode -- and is authoritative:
       any `X-User-Id` header is then ignored. Invalid → 401.
    2. Cookie-authenticated state-changing requests must carry an allowed
       `Origin` (the CSRF boundary, ADR 0022) → otherwise 403.
    3. With no credential: `session` mode → 401; `dev_header` mode (never in a
       production-like environment -- startup refuses) → today's placeholder.
    """
    credential = extract_credential(request, settings)
    if credential is not None:
        authenticated = await auth.authenticate(
            credential.token, clear_cookie=credential.via == "cookie"
        )
        if (
            credential.via == "cookie"
            and request.method in UNSAFE_METHODS
            and not origin_allowed(request, settings)
        ):
            raise OriginNotAllowedError()
        bind_user(authenticated.account.user_id)
        return AuthContext(
            user_id=authenticated.account.user_id,
            mode="session",
            via=credential.via,
            authenticated=authenticated,
        )
    if settings.effective_auth_mode == "dev_header":
        dev_user_id = _dev_header_identity(request.headers.get("x-user-id"), settings)
        bind_user(dev_user_id)
        return AuthContext(
            user_id=dev_user_id,
            mode="dev_header",
            via="dev_header",
        )
    raise AuthenticationRequiredError()


def get_project_service(
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> ProjectService:
    return ProjectService(db, settings, now_fn=clock)


def get_project_event_service(
    db: Annotated[Database, Depends(get_db)],
    graph: Annotated[GraphClient, Depends(get_client)],
    embeddings: Annotated[EmbeddingClient, Depends(get_embeddings)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> ProjectEventService:
    return ProjectEventService(
        db=db, graph=graph, embeddings=embeddings, settings=settings, now_fn=clock
    )


def get_git_ingest_service(
    db: Annotated[Database, Depends(get_db)],
    graph: Annotated[GraphClient, Depends(get_client)],
    embeddings: Annotated[EmbeddingClient, Depends(get_embeddings)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> GitIngestService:
    return GitIngestService(
        db=db,
        graph=graph,
        embeddings=embeddings,
        projects=projects,
        settings=settings,
        now_fn=clock,
    )


def get_project_query_service(
    db: Annotated[Database, Depends(get_db)],
    graph: Annotated[GraphClient, Depends(get_client)],
) -> ProjectQueryService:
    return ProjectQueryService(db=db, graph=graph)


def get_project_resume_service(
    db: Annotated[Database, Depends(get_db)],
    queries: Annotated[ProjectQueryService, Depends(get_project_query_service)],
    retrieval: Annotated[HybridRetrievalService, Depends(get_hybrid_retrieval_service)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> ProjectResumeService:
    return ProjectResumeService(db=db, queries=queries, retrieval=retrieval, now_fn=clock)


async def get_current_user_id(
    context: Annotated[AuthContext, Depends(get_auth_context)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    x_memory_space: Annotated[str | None, Header(alias="X-Memory-Space")] = None,
) -> str:
    """The ONE place every user-owned route gets its user id (ADR 0005's seam,
    kept). Routes, services, repositories and agent tools are unchanged: they
    still receive a plain `user_id: str` -- now server-derived.

    Phase 16: an account may select one of ITS OWN projects with
    `X-Memory-Space: <project_id>`. The server checks ownership and returns that
    project's memory-space id instead; a project the account does not own is a
    404, identical to a project that does not exist. Without the header nothing
    changes. Auth routes read `AuthContext.user_id` directly, so they are never
    redirected into a space. A project TOKEN is never accepted here (its `sbp_`
    prefix fails session verification): it is valid only on `/projects/...`."""
    if x_memory_space is None:
        return context.user_id
    return await projects.resolve_space(
        owner_user_id=context.user_id, project_id=x_memory_space.strip()
    )


async def get_project_scope(
    request: Request,
    project_id: str,
    settings: Annotated[Settings, Depends(get_app_settings)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectScope:
    """The project authority for `/projects/{project_id}/...` routes.

    * `Authorization: Bearer sbp_...`: the token IS the project. The path id must
      be the token's own project (or the alias `current`); any other id is a 404,
      so a token can never select another project.
    * Otherwise a normal account credential, and the project must be owned by it.
    The path id is only ever a lookup key inside an authorised set."""
    credential = extract_credential(request, settings)
    if credential is not None and credential.via == "bearer" and is_project_token(credential.token):
        scope = await projects.authenticate_token(credential.token)
        if project_id not in ("current", scope.project_id):
            raise NotFoundError("Project not found")
        bind_user(scope.owner_user_id)
        return scope
    context = await get_auth_context(request, settings, auth)
    if project_id == "current":
        raise NotFoundError("Project not found")  # `current` needs a project token
    return await projects.scope_for_owner(owner_user_id=context.user_id, project_id=project_id)


def require_project_scope(name: str) -> Callable:
    """A dependency that demands a token scope (`read`, `ingest`, `propose`). An
    account session has every scope; a project token only the ones it was given."""

    async def dependency(
        scope: Annotated[ProjectScope, Depends(get_project_scope)],
    ) -> ProjectScope:
        if name not in scope.scopes:
            raise ForbiddenError(f"This credential lacks the '{name}' scope")
        return scope

    dependency.__name__ = f"require_project_scope_{name}"
    return dependency


async def get_account_project_scope(
    scope: Annotated[ProjectScope, Depends(get_project_scope)],
) -> ProjectScope:
    """For operations a machine credential must never perform (minting or
    revoking tokens): an account session only."""
    if scope.via != "account":
        raise ForbiddenError("This operation requires an account session")
    return scope


def rate_limited(limit_class: LimitClass) -> Callable:
    """A per-user rate-limit dependency. Runs AFTER authentication, so
    unauthenticated floods get cheap 401s and never consume a user's quota."""

    async def dependency(
        context: Annotated[AuthContext, Depends(get_auth_context)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await limiter.enforce(limit_class, f"u:{context.user_id}")

    dependency.__name__ = f"rate_limited_{limit_class.value}"
    return dependency


def project_rate_limited(limit_class: LimitClass) -> Callable:
    """The rate limit for `/projects/{id}/...` routes. Keyed on the OWNING
    account (not the token, not the space), so minting many tokens or projects
    never multiplies an account's quota (docs/PHASE16_ARCHITECTURE.md section 9)."""

    async def dependency(
        scope: Annotated[ProjectScope, Depends(get_project_scope)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await limiter.enforce(limit_class, f"u:{scope.owner_user_id}")

    dependency.__name__ = f"project_rate_limited_{limit_class.value}"
    return dependency


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


def get_memory_explanation_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryExplanationService:
    return MemoryExplanationService(session, memory_service)


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


def get_graph_intelligence_service(
    client: Annotated[GraphClient, Depends(get_client)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    retrieval_service: Annotated[RetrievalService, Depends(get_retrieval_service)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    provenance_service: Annotated[ProvenanceService, Depends(get_provenance_service)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embeddings)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> GraphIntelligenceService:
    hybrid = HybridRetrievalService(
        session=session,
        memory_service=memory_service,
        retrieval_service=retrieval_service,
        embedding_client=embedding_client,
        settings=settings,
    )
    return GraphIntelligenceService(
        repository=GraphRepository(client),
        retrieval=retrieval_service,
        memory=memory_service,
        provenance=provenance_service,
        session=session,
        settings=settings,
        hybrid=hybrid,
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
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
ProjectReadScope = Annotated[ProjectScope, Depends(require_project_scope("read"))]
ProjectIngestScope = Annotated[ProjectScope, Depends(require_project_scope("ingest"))]
ProjectEventServiceDep = Annotated[ProjectEventService, Depends(get_project_event_service)]
GitIngestServiceDep = Annotated[GitIngestService, Depends(get_git_ingest_service)]
ProjectQueryServiceDep = Annotated[ProjectQueryService, Depends(get_project_query_service)]
ProjectResumeServiceDep = Annotated[ProjectResumeService, Depends(get_project_resume_service)]
ProjectProposeScope = Annotated[ProjectScope, Depends(require_project_scope("propose"))]
AccountProjectScope = Annotated[ProjectScope, Depends(get_account_project_scope)]
ProjectAnyScope = Annotated[ProjectScope, Depends(get_project_scope)]
AuthContextDep = Annotated[AuthContext, Depends(get_auth_context)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
DocumentStorageDep = Annotated[DocumentStorage, Depends(get_storage)]
ProvenanceServiceDep = Annotated[ProvenanceService, Depends(get_provenance_service)]
GraphIntelligenceServiceDep = Annotated[
    GraphIntelligenceService, Depends(get_graph_intelligence_service)
]
MemoryExplanationServiceDep = Annotated[
    MemoryExplanationService, Depends(get_memory_explanation_service)
]
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

# Per-class rate-limit dependencies (docs/PHASE9_PLAN.md section 11), attached
# to the routes the plan lists. Auth classes are enforced inside /auth handlers.
RATE_LIMIT_GRAPH = Depends(rate_limited(LimitClass.GRAPH))
PROJECT_RATE_LIMIT_SEARCH = Depends(project_rate_limited(LimitClass.SEARCH))
PROJECT_RATE_LIMIT_GRAPH = Depends(project_rate_limited(LimitClass.GRAPH))
PROJECT_RATE_LIMIT_WRITE = Depends(project_rate_limited(LimitClass.WRITE))
RATE_LIMIT_SEARCH = Depends(rate_limited(LimitClass.SEARCH))
RATE_LIMIT_AGENT = Depends(rate_limited(LimitClass.AGENT))
RATE_LIMIT_MESSAGE = Depends(rate_limited(LimitClass.MESSAGE))
RATE_LIMIT_INGEST = Depends(rate_limited(LimitClass.INGEST))
RATE_LIMIT_WRITE = Depends(rate_limited(LimitClass.WRITE))
