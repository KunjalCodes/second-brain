"""Agent event ingestion and projection (Phase 16, M6-M8, M26;
docs/PHASE16_ARCHITECTURE.md section 7).

    event --validate--> agent_events (Postgres, idempotent, auditable)
          --project--> existing governance pipeline --> canonical memory / graph

The two halves are deliberately separate transactions:

* **Recording** is Postgres-only and idempotent: a (session, event key) is recorded
  once however often it is sent, and a reused key with different content is a 409.
  If Neo4j is down the event is still accepted; nothing is lost.
* **Projection** turns the stored event into a canonical memory through
  `ExtractionRunner.apply_structured_candidate` (duplicate / conflict / supersession
  governance, provenance). It is idempotent by construction: the memory id is
  derived from the event id, the event row is locked while it is projected, and a
  memory that already exists under that id is adopted rather than re-created. A
  failed projection stays `pending`/`failed` and is retried (inline, by the sweeper,
  or by `POST /projects/{id}/events/project`) -- eventual consistency, never a lost
  or doubled fact.

Nothing here calls an LLM.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    ValidationError,
    is_infrastructure_error,
)
from app.db.engine import Database
from app.embeddings.base import EmbeddingClient
from app.extraction.runner import ExtractionRunner
from app.graph.connection import GraphClient
from app.llm.fake import OfflineLLMClient
from app.models.enums import SourceType
from app.models.extraction import DecisionType
from app.models.project_history import AgentEventType, ProjectionStatus
from app.projects.events import EventIn, event_fingerprint
from app.projects.projection import build_fact
from app.repositories.embedding_repository import EmbeddingRepository
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.project_history_repository import (
    AgentEventRepository,
    AgentSessionRepository,
)
from app.services.memory_service import MemoryService
from app.services.project_service import ProjectScope
from app.services.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)

#: Namespace for event -> memory ids (uuid5): same event, same memory identity.
MEMORY_NAMESPACE = uuid.UUID("5b3a2f0e-6d54-4c0e-9a41-16a0e5b1c001")
_ORDER_BACKLOG_LIMIT = 100


def projected_memory_id(space_id: str, event_id: str) -> str:
    """Deterministic and globally unique (Neo4j enforces `Memory.id` uniqueness
    across spaces, so the space is part of the name)."""
    return str(uuid.uuid5(MEMORY_NAMESPACE, f"{space_id}:{event_id}"))


@dataclass(frozen=True)
class IngestedEvent:
    event_id: str
    session_key: str
    event_type: AgentEventType
    #: `recorded` (new) or `duplicate` (this exact event was already recorded).
    status: str
    projection_status: ProjectionStatus
    memory_id: str | None
    note: str | None


class _Deferred(Exception):  # noqa: N818 - control flow, not an error
    """A prerequisite is not projected yet; retry later, in order."""


class ProjectEventService:
    def __init__(
        self,
        *,
        db: Database,
        graph: GraphClient,
        embeddings: EmbeddingClient,
        settings: Settings,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._db = db
        self._graph = graph
        self._embeddings = embeddings
        self._settings = settings
        self._now = now_fn
        self._new_id = id_fn

    # -- recording -----------------------------------------------------------------

    async def ingest(self, scope: ProjectScope, events: list[EventIn]) -> list[IngestedEvent]:
        """Record every event (in order), then try to project each. A projection that
        cannot complete does NOT fail the request: the event is safely recorded."""
        now = self._now()
        for e in events:  # fail the whole request before writing anything
            self._check_time(e, now)
        results: list[IngestedEvent] = []
        for event in events:
            recorded = await self._record(scope, event)
            status = recorded.projection_status
            memory_id, note = recorded.memory_id, recorded.note
            if status in ("pending", "failed"):
                status, memory_id, note = await self._project_safely(scope, recorded.event_id)
            results.append(
                IngestedEvent(
                    event_id=recorded.event_id,
                    session_key=event.session_id,
                    event_type=event.event_type,
                    status=recorded.status,
                    projection_status=status,
                    memory_id=memory_id,
                    note=note,
                )
            )
        return results

    def _check_time(self, event: EventIn, now: datetime) -> None:
        s = self._settings
        if event.timestamp > now + timedelta(seconds=s.project_event_max_future_seconds):
            raise ValidationError("timestamp is in the future")
        if event.timestamp < now - timedelta(days=s.project_event_max_age_days):
            raise ValidationError(
                f"timestamp is more than {s.project_event_max_age_days} days in the past"
            )

    async def _record(self, scope: ProjectScope, event: EventIn) -> IngestedEvent:
        fingerprint = event_fingerprint(event)
        key = event.event_id or fingerprint
        now = self._now()
        async with self._db.session() as pg:
            sessions = AgentSessionRepository(pg, user_id=scope.space_id)
            events = AgentEventRepository(pg, user_id=scope.space_id)

            if event.event_type is AgentEventType.SESSION_STARTED:
                previous = event.payload.get("continues_session")
                if previous and previous != event.session_id:
                    if await sessions.by_key(previous) is None:
                        raise ValidationError("continues_session does not exist in this project")
                elif previous:
                    raise ValidationError("a session cannot continue itself")
                session, _ = await sessions.get_or_create(
                    session_id=self._new_id(),
                    project_id=scope.project_id,
                    session_key=event.session_id,
                    agent_name=event.source,
                    started_at=event.timestamp,
                    now=now,
                )
            else:
                session = await sessions.by_key(event.session_id)
                if session is None:
                    raise NotFoundError("Session not found")  # also: a foreign session's key

            if (
                await events.count_for_session(session.id)
                >= self._settings.project_events_max_per_session
            ):
                # A replay of an already-recorded event must still succeed.
                existing = await events.insert_idempotent_lookup(session.id, key)
                if existing is None:
                    raise ConflictError("This session has reached its event limit")

            row, inserted = await events.insert_idempotent(
                event_id=self._new_id(),
                project_id=scope.project_id,
                session_id=session.id,
                event_type=event.event_type.value,
                event_key=key,
                occurred_at=event.timestamp,
                recorded_at=now,
                payload=event.payload,
                payload_sha256=fingerprint,
                source=event.source,
            )
            if not inserted:
                if row.payload_sha256 != fingerprint:
                    raise ConflictError(
                        "event_id was already used for a different event",
                        details={"event_id": key},
                    )
                return IngestedEvent(
                    row.id,
                    event.session_id,
                    event.event_type,
                    "duplicate",
                    row.projection_status,  # type: ignore[arg-type]
                    row.memory_id,
                    row.projection_error,
                )
            if session.status == "ended":
                # Raising rolls back the insert above.
                raise ConflictError("This session has ended; start a new session to continue")
            await sessions.record_event(session_id=session.id, at=event.timestamp)
            if event.event_type is AgentEventType.SESSION_ENDED:
                await sessions.end(
                    session_id=session.id,
                    ended_at=event.timestamp,
                    outcome=event.payload["outcome"],
                    summary=event.payload.get("summary"),
                )
            logger.info(
                "project_event_recorded project_id=%s session_id=%s event_id=%s type=%s",
                scope.project_id,
                session.id,
                row.id,
                event.event_type.value,
            )
            return IngestedEvent(
                row.id, event.session_id, event.event_type, "recorded", "pending", None, None
            )

    # -- projection ----------------------------------------------------------------

    async def project_backlog(self, *, space_id: str, limit: int) -> tuple[int, int]:
        """Retry this space's own unprojected events, oldest first. Returns
        (attempted, projected-or-skipped)."""
        async with self._db.session() as pg:
            ids = await AgentEventRepository(pg, user_id=space_id).unprojected(
                limit=limit, max_attempts=self._settings.project_projection_max_attempts
            )
        done = 0
        for event_id in ids:
            try:
                status, _, _ = await self.project_event(space_id=space_id, event_id=event_id)
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the rest
                logger.warning("project_backlog_failed reason=%s", type(exc).__name__)
                continue
            done += status in ("projected", "skipped")
        return len(ids), done

    async def _project_safely(
        self, scope: ProjectScope, event_id: str
    ) -> tuple[ProjectionStatus, str | None, str | None]:
        try:
            return await self.project_event(space_id=scope.space_id, event_id=event_id)
        except Exception as exc:  # noqa: BLE001 - recording succeeded; never fail the request
            logger.warning(
                "project_projection_deferred event_id=%s reason=%s", event_id, type(exc).__name__
            )
            return "pending", None, None

    async def project_event(
        self, *, space_id: str, event_id: str
    ) -> tuple[ProjectionStatus, str | None, str | None]:
        """Project one event (after the earlier events of its session, in order).
        Returns (projection_status, memory_id, note). Safe to call any number of times,
        concurrently, from any process."""
        async with self._db.session() as pg:
            current = await AgentEventRepository(pg, user_id=space_id).get(event_id)
        if current is None:
            raise NotFoundError("Event not found")
        if current.projection_status in ("projected", "skipped"):
            return current.projection_status, current.memory_id, current.projection_error  # type: ignore[return-value]

        # Order: earlier events of the same session first, so e.g. a decision that
        # supersedes an earlier one finds it already in the graph.
        async with self._db.session() as pg:
            earlier = await AgentEventRepository(pg, user_id=space_id).unprojected_before(
                session_id=current.session_id,
                recorded_seq=current.recorded_seq,
                limit=_ORDER_BACKLOG_LIMIT,
            )
        for earlier_id in earlier:
            status, _, _ = await self._project_one(space_id, earlier_id)
            if status in ("pending", "failed"):
                return "pending", None, "an earlier event of this session is not projected yet"
        return await self._project_one(space_id, event_id)

    async def _project_one(
        self, space_id: str, event_id: str
    ) -> tuple[ProjectionStatus, str | None, str | None]:
        async with self._db.session() as pg:
            events = AgentEventRepository(pg, user_id=space_id)
            event = await events.lock_for_projection(event_id)
            if event is None:  # projected meanwhile, or another worker holds the lock
                row = await events.get(event_id)
                if row is None:
                    raise NotFoundError("Event not found")
                return row.projection_status, row.memory_id, row.projection_error  # type: ignore[return-value]
            now = self._now()
            try:
                outcome = await self._apply(pg, space_id, event)
            except _Deferred as exc:
                exhausted = (
                    event.projection_attempts + 1 >= self._settings.project_projection_max_attempts
                )
                status = "failed" if exhausted else "pending"
                await events.mark(
                    event_id=event.id, status=status, memory_id=None, note=str(exc),
                    now=now, attempt=True,
                )  # fmt: skip
                return status, None, str(exc)  # type: ignore[return-value]
            except AppError as exc:
                if not is_infrastructure_error(exc):
                    # A deterministic domain rejection is a verdict, not an outage.
                    await events.mark(
                        event_id=event.id, status="skipped", memory_id=None,
                        note=f"rejected: {exc.message}", now=now, attempt=True,
                    )  # fmt: skip
                    return "skipped", None, f"rejected: {exc.message}"
                status = (
                    "failed"
                    if event.projection_attempts + 1
                    >= self._settings.project_projection_max_attempts
                    else "pending"
                )
                note = f"{type(exc).__name__}: retrying"
                await events.mark(
                    event_id=event.id, status=status, memory_id=None, note=note,
                    now=now, attempt=True,
                )  # fmt: skip
                logger.warning("project_projection_failed event_id=%s %s", event.id, note)
                return status, None, note  # type: ignore[return-value]
            await events.mark(
                event_id=event.id,
                status=outcome[0],
                memory_id=outcome[1],
                note=outcome[2],
                now=now,
                attempt=True,
            )
            return outcome  # type: ignore[return-value]

    async def _apply(self, pg, space_id: str, event) -> tuple[str, str | None, str | None]:  # noqa: ANN001
        sessions = AgentSessionRepository(pg, user_id=space_id)
        session = (await sessions.by_ids([event.session_id])).get(event.session_id)
        assert session is not None
        kind = AgentEventType(event.event_type)
        fact = build_fact(
            event_type=kind,
            payload=event.payload,
            session_key=session.session_key,
            event_id=event.id,
        )
        if fact.candidate is None:
            return "skipped", None, fact.skip_reason

        memory_id = projected_memory_id(space_id, event.id)
        memory_repo = MemoryRepository(self._graph)
        # Crash window: Neo4j committed but the status update did not. Adopt it.
        if await memory_repo.get(user_id=space_id, memory_id=memory_id) is not None:
            return "projected", memory_id, "already projected (adopted)"

        supersedes_id: str | None = None
        note = ""
        if fact.supersedes_title:
            prior = await AgentEventRepository(pg, user_id=space_id).decisions_titled(
                project_id=event.project_id,
                title=fact.supersedes_title,
                before_seq=event.recorded_seq,
            )
            projected = [p for p in prior if p.projection_status == "projected" and p.memory_id]
            if projected:
                supersedes_id = projected[0].memory_id
            elif prior:
                raise _Deferred("the decision this one replaces is not projected yet")
            else:
                note = " (replaced decision was never recorded)"

        memory_service = MemoryService(memory_repo, now_fn=self._now)
        retrieval = RetrievalService(
            EntityRepository(self._graph),
            max_hops=self._settings.graph_max_hops,
            now_fn=self._now,
        )
        runner = ExtractionRunner(
            graph_client=self._graph,
            llm_client=OfflineLLMClient(),  # never called on this path
            embedding_client=self._embeddings,
            settings=self._settings,
            now_fn=self._now,
        )
        applied = await runner.apply_structured_candidate(
            fact.candidate,
            stated_at=event.occurred_at,
            source_type=SourceType.AGENT_EVENT,
            source_reference=event.id,
            source_location_prefix=f"event:{event.event_type}",
            user_id=space_id,
            memory_service=memory_service,
            retrieval_service=retrieval,
            merge_repo=EntityMergeCandidateRepository(pg, user_id=space_id),
            embedding_repo=EmbeddingRepository(pg, user_id=space_id),
            db=self._db,
            embed_memory=fact.embed,
            supersedes_memory_id=supersedes_id,
            new_memory_id=memory_id,
        )
        if applied.decision in (DecisionType.IGNORE, DecisionType.INVALID):
            return "skipped", None, f"{applied.decision.value}: {applied.reason}"[:300]
        label = "" if applied.decision is DecisionType.NEW else f"{applied.decision.value}: "
        return "projected", applied.memory_id, (label + applied.reason + note)[:300] or None
