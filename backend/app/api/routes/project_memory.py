"""Project-memory query and proposal routes (Phase 16, M9-M19).

Everything is under `/projects/{project_id}/...` and authorised by
`get_project_scope`: a project token (bound to ONE project) or the owning account.
Reads need the `read` scope; `POST .../memory/propose` needs `propose`. All results are
bounded, LLM-free, and labelled with their evidence (see `schemas/project_query.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import (
    PROJECT_RATE_LIMIT_GRAPH,
    PROJECT_RATE_LIMIT_SEARCH,
    PROJECT_RATE_LIMIT_WRITE,
    GraphExplorationServiceDep,
    GraphIntelligenceServiceDep,
    HybridRetrievalServiceDep,
    ProjectEventServiceDep,
    ProjectProposeScope,
    ProjectQueryServiceDep,
    ProjectReadScope,
    ProjectResumeServiceDep,
    ProjectServiceDep,
    ProvenanceServiceDep,
    RetrievalServiceDep,
    get_clock,
)
from app.models.enums import EntityType
from app.projects.events import EventIn
from app.projects.shaping import entity_name_for, evidence_labels, source_refs
from app.schemas.graph import TemporalScope
from app.schemas.project import EventResult
from app.schemas.project_query import (
    AttemptsResponse,
    BugHistory,
    CommitContext,
    DecisionsResponse,
    EntityRef,
    ErrorsResponse,
    FileHistory,
    FixesResponse,
    HistoryResponse,
    ProjectEntityResponse,
    ProjectGraphResponse,
    ProjectSearchItem,
    ProjectSearchResponse,
    ProposalIn,
    RelatedChanges,
    ResumeContext,
    SessionDetail,
    SessionList,
    SourceView,
)
from app.schemas.provenance import ProvenanceView

router = APIRouter(prefix="/projects/{project_id}", tags=["project-memory"])

Limit = Annotated[int, Query(ge=1, le=50)]
#: No control characters (a NUL byte is not storable in PostgreSQL text and used to surface
#: as a 500; found by the security tests): a topic is plain text or it is a 422.
_PLAIN = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$"
Topic = Annotated[str | None, Query(min_length=1, max_length=200, pattern=_PLAIN)]
Cursor = Annotated[str | None, Query(max_length=256)]
FilePath = Annotated[str, Query(min_length=1, max_length=300, description="Repo-relative path")]
Kind = Literal[
    "file", "directory", "commit", "session", "branch", "symbol", "decision", "feature",
    "bug", "error", "fix", "dependency", "name",
]  # fmt: skip
GRAPH_NODE_CAP = 60
GRAPH_EDGE_CAP = 120


# -- search / entity / graph -------------------------------------------------------------------


@router.get(
    "/memory/search",
    dependencies=[PROJECT_RATE_LIMIT_SEARCH],
    response_model=ProjectSearchResponse,
    summary="Search this project's memory (hybrid: vector + graph + keyword)",
)
async def search_project_memory(
    scope: ProjectReadScope,
    service: HybridRetrievalServiceDep,
    query: Annotated[str, Query(min_length=1, max_length=500, pattern=_PLAIN)],
    limit: Limit = 10,
) -> ProjectSearchResponse:
    results = await service.search(user_id=scope.space_id, query=query, limit=limit)
    items = []
    for r in results:
        m, e = r.memory.memory, r.explanation
        items.append(
            ProjectSearchItem(
                memory_id=m.id,
                content=m.content,
                memory_type=m.memory_type.value,
                status=m.status.value,
                confidence=m.confidence,
                valid_from=m.valid_from,
                valid_until=m.valid_until,
                temporal_state=str(e.temporal_state),
                evidence=evidence_labels(r.memory.sources),
                sources=source_refs(r.memory.sources),
                entities=[
                    EntityRef(name=x.name, kind=x.entity_type.value) for x in r.memory.entities
                ],
                found_via=[str(c) for c in e.retrieved_via],
                score=round(e.final_score, 4),
            )
        )
    return ProjectSearchResponse(query=query, items=items)


@router.get(
    "/entity",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=ProjectEntityResponse,
    summary="One project entity (file, commit, decision, ...) with its context",
)
async def get_project_entity(
    scope: ProjectReadScope,
    retrieval: RetrievalServiceDep,
    intelligence: GraphIntelligenceServiceDep,
    kind: Kind,
    ref: Annotated[str, Query(min_length=1, max_length=300)],
    as_of: Annotated[datetime | None, Query()] = None,
) -> ProjectEntityResponse:
    name = entity_name_for(kind, ref)
    entity = await retrieval.find_entity_by_name(user_id=scope.space_id, name=name)
    context = await intelligence.entity_context(
        user_id=scope.space_id, entity_id=entity.id, as_of=as_of
    )
    return ProjectEntityResponse(entity_name=entity.name, context=context.model_dump(mode="json"))


@router.get(
    "/graph",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=ProjectGraphResponse,
    summary="A small, bounded neighbourhood around one project entity",
)
async def get_project_graph(
    scope: ProjectReadScope,
    retrieval: RetrievalServiceDep,
    exploration: GraphExplorationServiceDep,
    kind: Kind,
    ref: Annotated[str, Query(min_length=1, max_length=300)],
    hops: Annotated[int, Query(ge=1, le=2)] = 1,
    max_nodes: Annotated[int, Query(ge=1, le=GRAPH_NODE_CAP)] = 40,
    entity_type: Annotated[list[EntityType] | None, Query()] = None,
    as_of: Annotated[datetime | None, Query()] = None,
) -> ProjectGraphResponse:
    """Never the whole graph: at most 60 nodes / 120 edges / 2 hops, whatever is asked."""
    name = entity_name_for(kind, ref)
    entity = await retrieval.find_entity_by_name(user_id=scope.space_id, name=name)
    mode = TemporalScope.AS_OF if as_of else TemporalScope.CURRENT
    slice_ = await exploration.neighborhood(
        user_id=scope.space_id,
        entity_id=entity.id,
        hops=hops,
        max_nodes=max_nodes,
        max_edges=GRAPH_EDGE_CAP,
        temporal_scope=mode,
        as_of=as_of,
        entity_types=[t.value for t in entity_type] if entity_type else None,
    )
    return ProjectGraphResponse(root=entity.name, hops=hops, graph=slice_.model_dump(mode="json"))


# -- history -----------------------------------------------------------------------------------


@router.get(
    "/history",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=HistoryResponse,
    summary="What happened: events and commits, newest first by valid time",
)
async def get_project_history(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    topic: Topic = None,
    kinds: Annotated[list[Literal["events", "commits"]] | None, Query()] = None,
    types: Annotated[list[str] | None, Query(max_length=13)] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> HistoryResponse:
    return await service.history(
        scope, topic=topic, kinds=kinds, types=types, since=since, until=until,
        limit=limit, cursor=cursor,
    )  # fmt: skip


@router.get(
    "/files/history",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=FileHistory,
    summary="Everything known about one file: commits, sessions, decisions, bugs, tests, structure",
)
async def get_file_history(
    scope: ProjectReadScope, service: ProjectQueryServiceDep, path: FilePath, limit: Limit = 20
) -> FileHistory:
    return await service.file_history(scope, path=path, limit=limit)


@router.get(
    "/commits/{sha}",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=CommitContext,
    summary="One commit with parents, children, files and the sessions that mention it",
)
async def get_commit_context(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    sha: Annotated[str, Path(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")],
) -> CommitContext:
    return await service.commit_context(scope, commit=sha)


@router.get(
    "/related-changes",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=RelatedChanges,
    summary="Files that changed in the same commits as this one",
)
async def get_related_changes(
    scope: ProjectReadScope, service: ProjectQueryServiceDep, path: FilePath, limit: Limit = 10
) -> RelatedChanges:
    return await service.related_changes(scope, path=path, limit=limit)


@router.get(
    "/decisions",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=DecisionsResponse,
    summary="Decisions with their stated rationale, observed outcomes and supersession",
)
async def get_decisions(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    topic: Topic = None,
    file: Annotated[str | None, Query(max_length=300)] = None,
    include_superseded: bool = True,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> DecisionsResponse:
    return await service.decisions(
        scope, topic=topic, file=file, include_superseded=include_superseded,
        limit=limit, cursor=cursor,
    )  # fmt: skip


@router.get(
    "/bugs",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=BugHistory,
    summary="Bugs: discovery, errors, fixes (with stated outcomes) and validating tests",
)
async def get_bug_history(
    scope: ProjectReadScope, service: ProjectQueryServiceDep, topic: Topic = None, limit: Limit = 20
) -> BugHistory:
    return await service.bug_history(scope, topic=topic, limit=limit)


@router.get(
    "/errors",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=ErrorsResponse,
    summary="Errors encountered, grouped, with the fixes recorded for them",
)
async def get_related_errors(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    topic: Topic = None,
    file: Annotated[str | None, Query(max_length=300)] = None,
    limit: Limit = 20,
) -> ErrorsResponse:
    return await service.related_errors(scope, topic=topic, file=file, limit=limit)


@router.get(
    "/fixes",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=FixesResponse,
    summary="Fixes with their stated outcomes",
)
async def get_fix_history(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    topic: Topic = None,
    outcome: Literal["succeeded", "partial", "failed"] | None = None,
    limit: Limit = 20,
) -> FixesResponse:
    return await service.fix_history(scope, topic=topic, outcome=outcome, limit=limit)


@router.get(
    "/attempts",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=AttemptsResponse,
    summary="What was already tried: approaches with their explicit outcomes",
)
async def get_attempts(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    subject: Topic = None,
    limit: Limit = 20,
) -> AttemptsResponse:
    return await service.attempts(scope, subject=subject, limit=limit)


# -- resume --------------------------------------------------------------------------------------


@router.get(
    "/resume",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=ResumeContext,
    summary="Where I left off: the smallest useful context to continue earlier work",
)
async def get_resume_context(
    scope: ProjectReadScope,
    projects: ProjectServiceDep,
    service: ProjectResumeServiceDep,
    session_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    task: Topic = None,
    max_items: Annotated[int, Query(ge=1, le=10)] | None = None,
    include_unrelated: bool = False,
) -> ResumeContext:
    """A dedicated continuity operation, not a search. Deterministic, bounded, LLM-free.
    Unfinished work and failed attempts appear ONLY where they were explicitly recorded;
    a reason appears only where the agent stated one. `max_items` lowers every section's
    cap (it can never raise them); `task` biases decisions, bugs, errors and attempts
    toward a topic; `session_id` resumes a specific session instead of the latest;
    `include_unrelated=true` also returns history not tied to the recent session chain."""
    project = await projects.get_owned(
        owner_user_id=scope.owner_user_id, project_id=scope.project_id
    )
    return await service.resume(
        scope,
        project_name=project.name,
        session_id=session_id,
        task=task,
        max_items=max_items,
        include_unrelated=include_unrelated,
    )


# -- sessions and sources ----------------------------------------------------------------------


@router.get(
    "/sessions",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=SessionList,
    summary="Agent sessions, most recently active first",
)
async def list_sessions(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> SessionList:
    return await service.sessions(scope, limit=limit, cursor=cursor)


@router.get(
    "/sessions/{session_id}",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=SessionDetail,
    summary="One agent session: goal, files, decisions, errors, fixes, tests, commits, unfinished",
)
async def get_agent_session(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    session_id: Annotated[str, Path(min_length=1, max_length=64)],
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> SessionDetail:
    return await service.session(scope, session_id=session_id, after_seq=after_seq, limit=limit)


@router.get(
    "/sources/memory/{memory_id}",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=ProvenanceView,
    summary="Why does the system believe this memory? Its sources, resolved",
)
async def get_memory_source(
    scope: ProjectReadScope,
    provenance: ProvenanceServiceDep,
    memory_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> ProvenanceView:
    return await provenance.get_provenance(user_id=scope.space_id, memory_id=memory_id)


@router.get(
    "/sources/event/{event_id}",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=SourceView,
    summary="A recorded agent event, as stored",
)
async def get_event_source(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    event_id: Annotated[str, Path(min_length=36, max_length=36)],
) -> SourceView:
    return await service.source(scope, event_id=event_id)


@router.get(
    "/sources/commit/{sha}",
    dependencies=[PROJECT_RATE_LIMIT_GRAPH],
    response_model=SourceView,
    summary="A recorded commit, as stored",
)
async def get_commit_source(
    scope: ProjectReadScope,
    service: ProjectQueryServiceDep,
    sha: Annotated[str, Path(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")],
) -> SourceView:
    return await service.source(scope, commit=sha)


# -- proposals ---------------------------------------------------------------------------------


@router.post(
    "/memory/propose",
    dependencies=[PROJECT_RATE_LIMIT_WRITE],
    response_model=EventResult,
    summary="Propose a memory. It is recorded, then governed -- never written directly",
)
async def propose_project_memory_update(
    body: ProposalIn,
    scope: ProjectProposeScope,
    service: ProjectEventServiceDep,
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> EventResult:
    """The proposal becomes a MEMORY_PROPOSAL event in the named session and goes through
    the same duplicate / conflict / supersession governance as any other memory.
    Structural entities (files, commits, ...) cannot be proposed. Resending the same
    proposal is a no-op. A conflicting proposal is stored as `conflicting`, never
    overwriting what is on record."""
    payload = body.model_dump(mode="json", exclude={"session_id"}, exclude_none=True)
    event = EventIn.model_validate(
        {
            "session_id": body.session_id,
            "event_type": "memory_proposal",
            "timestamp": clock(),
            "payload": payload,
            "source": "propose_project_memory_update",
        }
    )
    (r,) = await service.ingest(scope, [event])
    return EventResult(
        event_id=r.event_id,
        session_id=r.session_key,
        event_type=r.event_type,
        status=r.status,  # type: ignore[arg-type]
        projection_status=r.projection_status,
        memory_id=r.memory_id,
        note=r.note,
    )
