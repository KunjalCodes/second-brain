"""Projects and project tokens (Phase 16).

Registration and listing use the account credential. Token minting and
revocation are account-only (a machine credential may never mint another). Every
`/projects/{project_id}/...` route resolves its authority through
`get_project_scope`: the id in the path is only a lookup key inside the set the
credential is authorised for.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    PROJECT_RATE_LIMIT_GRAPH,
    PROJECT_RATE_LIMIT_WRITE,
    AccountProjectScope,
    AuthContextDep,
    GitIngestServiceDep,
    ProjectAnyScope,
    ProjectEventServiceDep,
    ProjectIngestScope,
    ProjectServiceDep,
    rate_limited,
)
from app.projects.events import EventBatch
from app.projects.git_schemas import GitBatch, StructureBatch
from app.repositories.project_repository import ProjectRecord, ProjectTokenRecord
from app.schemas.project import (
    CommitResultView,
    EventResult,
    EventsResponse,
    GitCommitsResponse,
    ProjectBacklogResponse,
    ProjectListResponse,
    ProjectRegister,
    ProjectRegisterResponse,
    ProjectView,
    StructureResponse,
    TokenCreate,
    TokenCreated,
    TokenListResponse,
    TokenView,
)
from app.services.rate_limit_service import LimitClass

router = APIRouter(prefix="/projects", tags=["projects"])


def _view(p: ProjectRecord) -> ProjectView:
    return ProjectView(
        id=p.id,
        name=p.name,
        identity_kind=p.identity_kind,  # type: ignore[arg-type]
        identity_fingerprint=p.identity_key[:12],
        default_branch=p.default_branch,
        status=p.status,  # type: ignore[arg-type]
        created_at=p.created_at,
    )


def _token_view(t: ProjectTokenRecord) -> TokenView:
    return TokenView(
        id=t.id,
        label=t.label,
        scopes=list(t.scopes),  # type: ignore[arg-type]
        created_at=t.created_at,
        expires_at=t.expires_at,
        last_used_at=t.last_used_at,
        revoked_at=t.revoked_at,
    )


@router.post(
    "",
    dependencies=[Depends(rate_limited(LimitClass.WRITE))],
    response_model=ProjectRegisterResponse,
    summary="Register a project (idempotent per repository identity)",
)
async def register_project(
    body: ProjectRegister,
    context: AuthContextDep,
    service: ProjectServiceDep,
    response: Response,
) -> ProjectRegisterResponse:
    project, created = await service.register(
        owner_user_id=context.user_id,
        name=body.name,
        repository_url=body.repository_url,
        root_commits=body.root_commits,
        default_branch=body.default_branch,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ProjectRegisterResponse(project=_view(project), created=created)


@router.get(
    "",
    dependencies=[Depends(rate_limited(LimitClass.GRAPH))],
    response_model=ProjectListResponse,
    summary="List the account's projects",
)
async def list_projects(
    context: AuthContextDep,
    service: ProjectServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectListResponse:
    items = await service.list_for_owner(owner_user_id=context.user_id, limit=limit, offset=offset)
    return ProjectListResponse(items=[_view(p) for p in items], limit=limit, offset=offset)


@router.get(
    "/{project_id}",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=ProjectView,
    summary="One project (account session, or a token for that same project)",
)
async def get_project(
    scope: ProjectAnyScope,
    service: ProjectServiceDep,
) -> ProjectView:
    return _view(
        await service.get_owned(owner_user_id=scope.owner_user_id, project_id=scope.project_id)
    )


@router.post(
    "/{project_id}/tokens",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a scoped token for this project (account session only)",
)
async def create_token(
    body: TokenCreate,
    scope: AccountProjectScope,
    service: ProjectServiceDep,
) -> TokenCreated:
    record, secret = await service.create_token(
        scope=scope, label=body.label, scopes=list(body.scopes), ttl_days=body.ttl_days
    )
    return TokenCreated(token=_token_view(record), secret=secret)


@router.get(
    "/{project_id}/tokens",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=TokenListResponse,
    summary="This project's tokens (metadata only; account session only)",
)
async def list_tokens(
    scope: AccountProjectScope,
    service: ProjectServiceDep,
) -> TokenListResponse:
    return TokenListResponse(items=[_token_view(t) for t in await service.list_tokens(scope=scope)])


@router.post(
    "/{project_id}/tokens/{token_id}/revoke",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a token (account session only)",
)
async def revoke_token(
    token_id: str,
    scope: AccountProjectScope,
    service: ProjectServiceDep,
) -> Response:
    await service.revoke_token(scope=scope, token_id=token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{project_id}/events",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    response_model=EventsResponse,
    summary="Record agent events (idempotent; ingest scope)",
)
async def record_events(
    body: EventBatch,
    scope: ProjectIngestScope,
    service: ProjectEventServiceDep,
) -> EventsResponse:
    """The ingestion boundary for an external coding agent. The project comes from the
    credential, never the body; a malformed event rejects the whole request before any
    event is written; resending an event (or a whole batch) creates nothing new."""
    ingested = await service.ingest(scope, body.events)
    return EventsResponse(
        results=[
            EventResult(
                event_id=r.event_id,
                session_id=r.session_key,
                event_type=r.event_type,
                status=r.status,  # type: ignore[arg-type]
                projection_status=r.projection_status,
                memory_id=r.memory_id,
                note=r.note,
            )
            for r in ingested
        ]
    )


@router.post(
    "/{project_id}/events/project",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    response_model=ProjectBacklogResponse,
    summary="Retry projecting this project's pending events (ingest scope)",
)
async def project_backlog(
    scope: ProjectIngestScope,
    service: ProjectEventServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ProjectBacklogResponse:
    attempted, projected = await service.project_backlog(space_id=scope.space_id, limit=limit)
    return ProjectBacklogResponse(
        attempted=attempted, projected=projected, still_pending=attempted - projected
    )


@router.post(
    "/{project_id}/git/commits",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    response_model=GitCommitsResponse,
    summary="Record commits from the local collector (idempotent by hash; ingest scope)",
)
async def record_commits(
    body: GitBatch,
    scope: ProjectIngestScope,
    service: GitIngestServiceDep,
) -> GitCommitsResponse:
    """Structured history only: no diffs, no file contents, no raw author emails. The
    batch must belong to the project's repository (checked against its identity)."""
    results = await service.ingest_commits(scope, body)
    return GitCommitsResponse(
        results=[
            CommitResultView(
                sha=r.sha,
                status=r.status,  # type: ignore[arg-type]
                projection_status=r.projection_status,
                note=r.note,
            )
            for r in results
        ]
    )


@router.post(
    "/{project_id}/git/structure",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    response_model=StructureResponse,
    summary="Record a file-structure snapshot read from an ingested commit (ingest scope)",
)
async def record_structure(
    body: StructureBatch,
    scope: ProjectIngestScope,
    service: GitIngestServiceDep,
) -> StructureResponse:
    r = await service.ingest_structure(scope, body)
    return StructureResponse(
        files_received=r.files_received,
        memories_created=r.memories_created,
        memories_existing=r.memories_existing,
        memories_superseded=r.memories_superseded,
        files_stale=r.files_stale,
    )
