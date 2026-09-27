"""Phase 5 agent memory tools (docs/PHASE5_PLAN.md section 3).

Every tool wraps an existing service -- `HybridRetrievalService`,
`RetrievalService`, `MemoryService`, `ProvenanceService` -- never a raw
Cypher/SQL statement, and never a reimplementation of retrieval or business
logic already built in Phases 1-4.

**Security invariant, checked structurally, not just by convention:**
`user_id` is a Python parameter on every dispatch function, supplied by
`AgentService` from the authenticated request context. It is never read from
`tool_args`, and no tool's Args schema (`app.models.agent`) declares a
`user_id` field at all -- there is no code path by which a value the model
emits could reach a service call as `user_id`. `test_agent_models.py`
statically asserts no schema has the field; the tests here additionally prove
that a tool_args value naming another user's real entity/memory id still
resolves within the CALLER's own subgraph only (inherited for free from every
service's own existing user-scoping, not a new isolation surface).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import (
    AppError,
    GraphUnavailableError,
    NotFoundError,
    VectorSearchUnavailableError,
)
from app.db.engine import Database
from app.embeddings.base import EmbeddingClient
from app.extraction.runner import ExtractionRunner
from app.graph.connection import GraphClient
from app.llm.base import LLMClient
from app.models.agent import (
    TOOL_ARGS_SCHEMAS,
    GetEntityArgs,
    GetEntityStateArgs,
    GetMemoryHistoryArgs,
    GetSourceArgs,
    ProposeMemoryUpdateArgs,
    SearchMemoryArgs,
    ToolName,
    TraverseGraphArgs,
)
from app.models.conversation import Message
from app.models.domain import MemoryDetail
from app.models.enums import SourceType
from app.models.extraction import CandidateMemory, DecisionType, Span
from app.repositories.embedding_repository import EmbeddingRepository
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.extraction_queue import ExtractionQueue
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
    candidate_hash,
)
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.semantic.query_understanding import QueryIntent, TemporalIntent
from app.services.hybrid_retrieval_service import (
    HybridRetrievalService,
    HybridSearchResult,
    _temporal_state,
)
from app.services.memory_history import walk_memory_history
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import RetrievalService

#: Decisions that mean nothing was written and no citation applies -- see
#: `propose_memory_update`'s status mapping.
_NO_OP_DECISIONS = frozenset({DecisionType.IGNORE, DecisionType.INVALID})

#: Review needed downstream (PHASE4_PLAN.md section 6) -- identical set to
#: `ExtractionRunner.run`'s own review-candidate condition, duplicated here
#: only because this tool calls `_apply_candidate` directly rather than
#: through `run`, which owns that write for the passive pipeline.
_NEEDS_REVIEW = frozenset(
    {DecisionType.LIKELY_DUPLICATE, DecisionType.FLAGGED, DecisionType.CONFLICTING}
)

#: A tool result never includes more than this many chained/history memories
#: (get_memory_history) -- a bounded traversal for the same reason every
#: other graph-adjacent bound in this codebase is bounded: a chain this long
#: has never been observed and is worth surfacing as unusual, not silently
#: truncated forever nor allowed to grow unbounded.
_MAX_HISTORY_CHAIN = 20


class ToolStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass(frozen=True)
class ToolResult:
    """What a tool dispatch call produces. `data` is the compact, bounded,
    JSON-serializable payload the agent prompt renders (never a raw
    `MemoryDetail`/`EntityDetail`/domain object); `memory_ids` is tracked
    independently by the loop for citation validation (PHASE5_PLAN.md
    section 9) -- the agent's own prompt text is never trusted to compute
    this correctly.
    """

    status: ToolStatus
    data: dict[str, Any] = field(default_factory=dict)
    memory_ids: list[str] = field(default_factory=list)
    message: str | None = None


@dataclass(frozen=True)
class ToolDependencies:
    """The existing services one agent turn needs, bundled once per turn by
    `AgentService` and threaded through every dispatch call -- no tool
    constructs its own service or repository.

    The fields below `now_fn`/`id_fn` are needed only by `propose_memory_update`
    (PHASE5_PLAN.md section 7) -- the read-only tools never touch them, which
    is why every existing read-only tool test constructs `ToolDependencies`
    without supplying them. `db`/`graph_client`/`llm_client`/`embedding_client`
    let the write path build a real `ExtractionRunner` exactly like the passive
    extraction pipeline does; `conversation_id`/`target_message`/`window_text`
    are this turn's own context, attached by the API route (never derivable
    from a plain dependency-injection function, unlike everything above).
    """

    hybrid_retrieval: HybridRetrievalService
    retrieval: RetrievalService
    memory: MemoryService
    provenance: ProvenanceService
    settings: Settings
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
    id_fn: Callable[[], str] = lambda: str(uuid.uuid4())  # noqa: E731
    db: Database | None = None
    graph_client: GraphClient | None = None
    llm_client: LLMClient | None = None
    embedding_client: EmbeddingClient | None = None
    conversation_id: str | None = None
    target_message: Message | None = None
    window_text: str | None = None


def _compact_memory(
    detail: MemoryDetail, *, now: datetime, found_via: str | None = None
) -> dict[str, Any]:
    """The fixed, compact per-memory shape every tool that surfaces a memory
    renders (PHASE5_PLAN.md section 10) -- content, entities, temporal state,
    confidence/status, never the raw domain object's full field set.
    """
    out: dict[str, Any] = {
        "memory_id": detail.memory.id,
        "content": detail.memory.content,
        "entities": [e.name for e in detail.entities],
        "temporal_state": _temporal_state(detail.memory.status, detail.memory.valid_until, now),
        "confidence": detail.memory.confidence,
        "status": detail.memory.status.value,
    }
    if detail.memory.valid_from is not None:
        out["valid_from"] = detail.memory.valid_from.isoformat()
    if detail.memory.valid_until is not None:
        out["valid_until"] = detail.memory.valid_until.isoformat()
    if detail.duplicate_of:
        out["duplicate_of"] = list(detail.duplicate_of)
    if detail.conflicts_with:
        out["conflicts_with"] = list(detail.conflicts_with)
    if found_via:
        out["found_via"] = found_via
    return out


def _found_via_text(result: HybridSearchResult) -> str:
    """A short, human-readable rendering of `RetrievalExplanation` for the
    agent's own reasoning -- never the raw numeric scores (PHASE5_PLAN.md
    section 10), and never a fabricated explanation: every word here traces
    back to a field the retrieval layer actually computed.
    """
    explanation = result.explanation
    parts = []
    for channel in explanation.retrieved_via:
        if channel == "graph" and explanation.graph_distance is not None:
            path = explanation.graph_path or []
            trail = " -> ".join(step.entity_name for step in path)
            parts.append(f"graph ({explanation.graph_distance} hop(s) via {trail})")
        else:
            parts.append(channel)
    return ", ".join(parts) if parts else "unknown"


# ---------------------------------------------------------------------------
# Tool implementations. Each takes its own validated Args model, `user_id`
# (always server-supplied), and `deps`. None issues Cypher/SQL.
# ---------------------------------------------------------------------------


async def search_memory(
    args: SearchMemoryArgs, *, user_id: str, deps: ToolDependencies
) -> ToolResult:
    intent_override: QueryIntent | None = None
    if args.temporal_scope == "current":
        intent_override = QueryIntent(TemporalIntent.CURRENT)
    elif args.temporal_scope == "historical":
        intent_override = QueryIntent(TemporalIntent.HISTORICAL)
    elif args.temporal_scope == "as_of":
        assert args.as_of is not None  # enforced by SearchMemoryArgs's own validator
        intent_override = QueryIntent(TemporalIntent.AS_OF, as_of=args.as_of)

    results = await deps.hybrid_retrieval.search(
        user_id=user_id, query=args.query, limit=args.top_k, intent_override=intent_override
    )
    memories = [
        _compact_memory(r.memory, now=deps.now_fn(), found_via=_found_via_text(r)) for r in results
    ]
    return ToolResult(
        status=ToolStatus.OK if memories else ToolStatus.NOT_FOUND,
        data={"memories": memories},
        memory_ids=[m["memory_id"] for m in memories],
        message=None if memories else "No memories matched this query.",
    )


async def get_entity(args: GetEntityArgs, *, user_id: str, deps: ToolDependencies) -> ToolResult:
    """Tries a name lookup first (the common case -- the model names an
    entity in prose), falling back to an id lookup. Both paths already
    resolve through `RetrievalService.resolve_entity` internally (the Phase 4
    follow-up fix), so an old/merged name or id transparently returns the
    canonical entity -- no redirect logic is duplicated here.
    """
    try:
        entity = await deps.retrieval.find_entity_by_name(user_id=user_id, name=args.name_or_id)
        detail = await deps.retrieval.get_entity(user_id=user_id, entity_id=entity.id)
    except NotFoundError:
        try:
            detail = await deps.retrieval.get_entity(user_id=user_id, entity_id=args.name_or_id)
        except NotFoundError:
            return ToolResult(
                status=ToolStatus.NOT_FOUND,
                message=f"No entity found matching {args.name_or_id!r}.",
            )
    data = {
        "entity_id": detail.entity.id,
        "name": detail.entity.name,
        "entity_type": detail.entity.entity_type.value,
        "aliases": list(detail.entity.aliases),
        "memory_count": detail.memory_count,
        "relationship_count": detail.relationship_count,
    }
    return ToolResult(status=ToolStatus.OK, data=data)


async def traverse_graph(
    args: TraverseGraphArgs, *, user_id: str, deps: ToolDependencies
) -> ToolResult:
    try:
        nodes = await deps.retrieval.get_neighbourhood_with_paths(
            user_id=user_id,
            entity_id=args.entity_id,
            hops=args.max_hops,
            max_nodes=deps.settings.retrieval_graph_max_nodes,
        )
    except NotFoundError:
        return ToolResult(
            status=ToolStatus.NOT_FOUND, message=f"No entity found for id {args.entity_id!r}."
        )

    if args.relationship_filter:
        nodes = [
            n for n in nodes if any(step.predicate == args.relationship_filter for step in n.path)
        ]

    data = {
        "nodes": [
            {
                "entity_id": n.entity.id,
                "name": n.entity.name,
                "hops": n.hops,
                "path": [
                    {
                        "entity_id": step.entity_id,
                        "entity_name": step.entity_name,
                        "predicate": step.predicate,
                        "direction": step.direction,
                    }
                    for step in n.path
                ],
            }
            for n in nodes
        ]
    }
    return ToolResult(
        status=ToolStatus.OK if nodes else ToolStatus.NOT_FOUND,
        data=data,
        message=None if nodes else "No neighbours found within the requested hop budget.",
    )


async def get_entity_state(
    args: GetEntityStateArgs, *, user_id: str, deps: ToolDependencies
) -> ToolResult:
    """Current-vs-historical, computed by the services layer
    (`RetrievalService.get_entity_state`) -- never left for the model to
    infer from a raw relationship list (PHASE5_PLAN.md section 6's explicit
    "do not let the LLM invent temporal state" requirement).
    """
    try:
        state = await deps.retrieval.get_entity_state(
            user_id=user_id, entity_id=args.entity_id, as_of=args.as_of
        )
    except NotFoundError:
        return ToolResult(
            status=ToolStatus.NOT_FOUND, message=f"No entity found for id {args.entity_id!r}."
        )

    def _rel(v: Any) -> dict[str, Any]:
        return {
            "predicate": v.relationship.predicate,
            "target_entity_id": v.target_entity.id,
            "target_entity_name": v.target_entity.name,
            "status": v.relationship.status.value,
            "valid_from": v.relationship.valid_from.isoformat()
            if v.relationship.valid_from
            else None,
            "valid_until": v.relationship.valid_until.isoformat()
            if v.relationship.valid_until
            else None,
        }

    data = {
        "entity_id": state.entity.id,
        "name": state.entity.name,
        "as_of": state.as_of.isoformat(),
        "current": [_rel(v) for v in state.current],
        "historical": [_rel(v) for v in state.historical],
    }
    return ToolResult(status=ToolStatus.OK, data=data)


async def get_memory_history(
    args: GetMemoryHistoryArgs, *, user_id: str, deps: ToolDependencies
) -> ToolResult:
    """Walks `supersedes`/`superseded_by`/`duplicate_of`/`duplicates` outward
    from one memory to assemble its full chronological/identity chain --
    the shared walk in `app.services.memory_history` (Phase 8 extraction,
    also used by the graph timeline endpoint), no new Cypher.
    """
    try:
        walk = await walk_memory_history(
            deps.memory, user_id=user_id, memory_id=args.memory_id, max_chain=_MAX_HISTORY_CHAIN
        )
    except NotFoundError:
        return ToolResult(
            status=ToolStatus.NOT_FOUND, message=f"No memory found with id {args.memory_id!r}."
        )

    now = deps.now_fn()
    chain = {detail.memory.id: _compact_memory(detail, now=now) for detail in walk.details}
    return ToolResult(
        status=ToolStatus.OK, data={"chain": list(chain.values())}, memory_ids=list(chain)
    )


async def get_source(args: GetSourceArgs, *, user_id: str, deps: ToolDependencies) -> ToolResult:
    try:
        view = await deps.provenance.get_provenance(user_id=user_id, memory_id=args.memory_id)
    except NotFoundError:
        return ToolResult(
            status=ToolStatus.NOT_FOUND, message=f"No memory found with id {args.memory_id!r}."
        )

    sources = []
    for sp in view.sources:
        entry: dict[str, Any] = {
            "source_type": sp.source.source_type.value,
            "reference": sp.source.reference,
        }
        if sp.message is not None:
            entry["message_content"] = sp.message.content
            entry["message_role"] = sp.message.role.value
        if sp.document is not None:
            entry["document_filename"] = sp.document.filename
        if sp.document_chunk is not None:
            entry["document_chunk_content"] = sp.document_chunk.content
            if sp.document_chunk.page_start is not None:
                entry["document_page"] = (
                    sp.document_chunk.page_start
                    if sp.document_chunk.page_start == sp.document_chunk.page_end
                    else f"{sp.document_chunk.page_start}-{sp.document_chunk.page_end}"
                )
            if sp.document_chunk.heading_context is not None:
                entry["document_section"] = sp.document_chunk.heading_context
        sources.append(entry)

    return ToolResult(
        status=ToolStatus.OK,
        data={"memory_id": args.memory_id, "sources": sources},
        memory_ids=[args.memory_id],
        message=None if sources else "No provenance recorded for this memory.",
    )


async def propose_memory_update(
    args: ProposeMemoryUpdateArgs, *, user_id: str, deps: ToolDependencies
) -> ToolResult:
    """The one tool that writes. Never touches Neo4j/PostgreSQL directly --
    it builds a `CandidateMemory` from the model's proposal and routes it
    through the exact same pipeline passive extraction uses:
    `ExtractionRunner._apply_candidate` (grounding -> entity resolution ->
    duplicate/conflict classification -> temporal decision -> provenance ->
    canonical Neo4j write), under a real, FK-satisfying `extraction_jobs` row
    so the write is audited exactly like every other memory write
    (PHASE5_PLAN.md section 7). The agent never bypasses any of these steps.

    Feature-gated by `AGENT_WRITE_ENABLED`: while disabled, this always
    returns REJECTED without touching either database.

    `source_message_id`/`source_span` are never taken from the model -- they
    are `deps.target_message`, the real, already-persisted user message this
    turn is grounded against (the same server-supplied-context pattern every
    other tool uses for `user_id`). A proposal not actually supported by this
    conversation is caught the same way passive extraction catches one:
    `check_grounding`'s entity-mention verification against
    `deps.target_message.content`/`deps.window_text`, inside `_apply_candidate`.
    """
    if not deps.settings.agent_write_enabled:
        return ToolResult(
            status=ToolStatus.REJECTED,
            message="Memory writes are disabled for the agent (AGENT_WRITE_ENABLED=false).",
        )
    if (
        deps.db is None
        or deps.graph_client is None
        or deps.llm_client is None
        or deps.embedding_client is None
        or deps.conversation_id is None
        or deps.target_message is None
        or deps.window_text is None
    ):
        return ToolResult(
            status=ToolStatus.ERROR,
            message="Agent write path is not fully configured for this turn.",
        )

    target_message = deps.target_message
    now = deps.now_fn()
    candidate = CandidateMemory(
        statement=args.statement,
        memory_type=args.memory_type,
        persistence=args.persistence,
        confidence=args.confidence,
        entities=args.entities,
        relationships=args.relationships,
        temporal=args.temporal,
        source_message_id=target_message.id,
        source_span=Span(start=0, end=len(target_message.content)),
        reasoning=args.reasoning,
    )
    payload = candidate.model_dump(mode="json")
    chash = candidate_hash(payload)

    # A distinct extractor_version namespaces agent-authored jobs away from
    # passive extraction's own job for this same message -- the two pipelines
    # never contend for the same (target_message_id, extractor_version)
    # idempotency key (PHASE5_PLAN.md section 7).
    extractor_version = f"agent:{deps.settings.extractor_version}"
    async with deps.db.session() as session:
        job = await ExtractionJobRepository(session, user_id=user_id).create_job(
            job_id=deps.id_fn(),
            conversation_id=deps.conversation_id,
            target_message_id=target_message.id,
            window_start_sequence=target_message.sequence,
            window_end_sequence=target_message.sequence,
            extractor_version=extractor_version,
            now=now,
            claimed_by=f"agent-tool:{deps.id_fn()}",
        )
        # Idempotency: a retried identical proposal against a job that already
        # applied it (e.g. a repeated tool call within the loop's own retry/
        # repair path) must not write a second memory -- report what already
        # happened instead of re-running `_apply_candidate`.
        existing = next(
            (
                r
                for r in await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
                    extraction_job_id=job.id
                )
                if r.candidate_hash == chash
            ),
            None,
        )
    if existing is not None:
        return ToolResult(
            status=ToolStatus.OK if existing.memory_id else ToolStatus.REJECTED,
            data={
                "decision": existing.decision.value,
                "reason": existing.decision_reason,
                "memory_id": existing.memory_id,
                "idempotent": True,
            },
            memory_ids=[existing.memory_id] if existing.memory_id else [],
        )

    runner = ExtractionRunner(
        graph_client=deps.graph_client,
        llm_client=deps.llm_client,
        embedding_client=deps.embedding_client,
        settings=deps.settings,
        now_fn=deps.now_fn,
        id_fn=deps.id_fn,
    )
    try:
        async with deps.db.session() as session:
            applied = await runner._apply_candidate(  # noqa: SLF001 - the reuse point PHASE5_PLAN.md section 7 designates
                candidate,
                target_content=target_message.content,
                source_type=SourceType.MESSAGE,
                source_reference=target_message.id,
                source_location_prefix=None,
                window_text=deps.window_text,
                user_id=user_id,
                memory_service=deps.memory,
                retrieval_service=deps.retrieval,
                merge_repo=EntityMergeCandidateRepository(session, user_id=user_id),
                embedding_repo=EmbeddingRepository(session, user_id=user_id),
                db=deps.db,
                now=now,
            )
            candidates_repo = ExtractionCandidateRepository(session, user_id=user_id)
            record, created = await candidates_repo.record_candidate(
                candidate_id=deps.id_fn(),
                extraction_job_id=job.id,
                source_message_id=target_message.id,
                ordinal=0,
                payload=payload,
                decision=applied.decision,
                decision_reason=applied.reason,
                confidence=candidate.confidence,
                now=now,
            )
            if created and applied.memory_id is not None:
                await candidates_repo.mark_applied(
                    candidate_id=record.id, memory_id=applied.memory_id, now=now
                )
            if (
                applied.memory_id is not None
                and applied.target_memory_id is not None
                and applied.decision in _NEEDS_REVIEW
            ):
                await MemoryReviewCandidateRepository(session, user_id=user_id).record_candidate(
                    candidate_id=deps.id_fn(),
                    kind=applied.decision.value.lower(),
                    memory_id_a=applied.memory_id,
                    memory_id_b=applied.target_memory_id,
                    source_extraction_candidate_id=record.id,
                    evidence={"reason": applied.reason, "similarity": applied.evidence_score},
                    score=applied.evidence_score,
                    now=now,
                )
            await ExtractionQueue(session).mark_done(
                job_id=job.id,
                owner=job.locked_by,
                provider="agent",
                model="propose_memory_update",
                prompt_version=extractor_version,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                candidate_count=1,
                now=now,
            )
    except Exception as exc:
        # Phase 11: an agent-tool job is claimed at creation and processed only
        # here. If applying fails (e.g. Neo4j unavailable), end it explicitly --
        # otherwise its lease would expire and a background worker would run
        # passive extraction on it. The tool call itself reports the failure.
        async with deps.db.session() as session:
            await ExtractionQueue(session).mark_failed(
                job_id=job.id,
                owner=job.locked_by,
                error_code=getattr(exc, "code", "internal_error"),
                error_detail="the agent's memory proposal could not be applied",
                permanent=True,
                next_attempt_at=None,
                now=now,
            )
        raise

    return ToolResult(
        status=ToolStatus.REJECTED if applied.decision in _NO_OP_DECISIONS else ToolStatus.OK,
        data={
            "decision": applied.decision.value,
            "reason": applied.reason,
            "memory_id": applied.memory_id,
        },
        memory_ids=[applied.memory_id] if applied.memory_id else [],
        message=applied.reason if applied.decision in _NO_OP_DECISIONS else None,
    )


ToolFn = Callable[[BaseModel, str, ToolDependencies], Awaitable[ToolResult]]

TOOL_DISPATCH: dict[str, Callable[..., Awaitable[ToolResult]]] = {
    "search_memory": search_memory,
    "get_entity": get_entity,
    "traverse_graph": traverse_graph,
    "get_entity_state": get_entity_state,
    "get_memory_history": get_memory_history,
    "get_source": get_source,
    "propose_memory_update": propose_memory_update,
}

#: One retry for a genuine timeout, mirroring the LLM adapter's own
#: "immediate retry for transient failures only" pattern (PHASE5_PLAN.md
#: section 18) -- never retried more than once, and never for a non-timeout
#: failure (a NotFoundError-shaped absence is not a transient condition).
_TOOL_TIMEOUT_RETRIES = 1


async def dispatch_tool(
    tool_name: ToolName,
    raw_args: dict[str, Any],
    *,
    user_id: str,
    deps: ToolDependencies,
    timeout: float,
) -> ToolResult:
    """The one entry point `AgentService`'s loop calls. Validates
    `raw_args` against the named tool's own Args schema (never trusts a
    `ToolCallStep.tool_args` dict directly), enforces a per-call timeout, and
    maps every failure into a `ToolResult` the model can react to -- a tool
    never raises out of this function.
    """
    schema = TOOL_ARGS_SCHEMAS.get(tool_name)
    dispatch_fn = TOOL_DISPATCH.get(tool_name)
    if schema is None or dispatch_fn is None:
        return ToolResult(
            status=ToolStatus.ERROR, message=f"Unknown or unavailable tool {tool_name!r}."
        )

    try:
        args = schema.model_validate(raw_args)
    except ValidationError as exc:
        return ToolResult(
            status=ToolStatus.ERROR,
            message=f"Invalid arguments for {tool_name}: {exc.errors()[0]['msg']}",
        )

    attempts = 0
    while True:
        attempts += 1
        try:
            return await asyncio.wait_for(
                dispatch_fn(args, user_id=user_id, deps=deps), timeout=timeout
            )
        except TimeoutError:
            if attempts > _TOOL_TIMEOUT_RETRIES:
                return ToolResult(
                    status=ToolStatus.TIMEOUT,
                    message=f"{tool_name} timed out after {timeout:.0f}s.",
                )
            continue
        except (GraphUnavailableError, VectorSearchUnavailableError) as exc:
            return ToolResult(status=ToolStatus.UNAVAILABLE, message=exc.message)
        except AppError as exc:
            return ToolResult(status=ToolStatus.ERROR, message=exc.message)
