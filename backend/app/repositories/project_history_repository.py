"""Agent-event, session and Git-commit persistence (Phase 16).

Every repository here is a `UserScopedRepository` whose `user_id` is the PROJECT'S
MEMORY-SPACE id, exactly like every other user-owned table: the isolation filter is
applied by the base class and audited by `tests/unit/test_sql_safety.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, and_, cast, func, or_
from sqlalchemy.dialects.postgresql import insert

from app.db.models import AgentEvent, AgentSession, GitCommit
from app.repositories.sql_base import UserScopedRepository


def like_pattern(topic: str) -> str:
    """`%topic%` with LIKE wildcards in the topic escaped, so a topic is only ever a
    literal substring (the pattern is also a bound parameter, never SQL text)."""
    return "%" + topic.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class GitCommitRepository(UserScopedRepository):
    async def get_many(self, *, commit_ids: list[str]) -> dict[str, GitCommit]:
        if not commit_ids:
            return {}
        rows = (
            await self._session.execute(self._select(GitCommit).where(GitCommit.id.in_(commit_ids)))
        ).scalars()
        return {r.id: r for r in rows}

    async def by_sha(self, sha: str) -> GitCommit | None:
        return (
            await self._session.execute(self._select(GitCommit).where(GitCommit.sha == sha))
        ).scalar_one_or_none()

    async def get(self, commit_id: str) -> GitCommit | None:
        return (
            await self._session.execute(self._select(GitCommit).where(GitCommit.id == commit_id))
        ).scalar_one_or_none()

    async def insert_idempotent(self, *, values: dict) -> tuple[GitCommit, bool]:
        """Insert, or return the commit already recorded under this sha. The unique
        constraint (space, sha) decides; the flag says which happened."""
        inserted = (
            await self._session.execute(
                insert(GitCommit)
                .values(user_id=self._user_id, projection_status="pending", **values)
                .on_conflict_do_nothing(constraint="git_commits_sha_unique")
                .returning(GitCommit.id)
            )
        ).scalar_one_or_none()
        row = await self.by_sha(values["sha"])
        assert row is not None
        return row, inserted is not None

    async def timeline(
        self,
        *,
        project_id: str,
        topic: str | None,
        since: datetime | None,
        until: datetime | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> list[GitCommit]:
        q = self._select(GitCommit).where(GitCommit.project_id == project_id)
        if topic:
            pat = like_pattern(topic)
            q = q.where(
                or_(
                    GitCommit.subject.ilike(pat, escape="\\"),
                    cast(GitCommit.files, Text).ilike(pat, escape="\\"),
                )
            )
        if since:
            q = q.where(GitCommit.committed_at >= since)
        if until:
            q = q.where(GitCommit.committed_at < until)
        if before:
            at, rid = before
            q = q.where(
                or_(
                    GitCommit.committed_at < at,
                    and_(GitCommit.committed_at == at, GitCommit.id < rid),
                )
            )
        q = q.order_by(GitCommit.committed_at.desc(), GitCommit.id.desc()).limit(limit)
        return list((await self._session.execute(q)).scalars())

    async def touching_file(self, *, project_id: str, path: str, limit: int) -> list[GitCommit]:
        q = (
            self._select(GitCommit)
            .where(
                GitCommit.project_id == project_id,
                or_(
                    GitCommit.files.contains([{"path": path}]),
                    GitCommit.files.contains([{"old_path": path}]),
                ),
            )
            .order_by(GitCommit.committed_at.desc(), GitCommit.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(q)).scalars())

    async def by_sha_prefix(
        self, *, project_id: str, prefix: str, limit: int = 2
    ) -> list[GitCommit]:
        q = (
            self._select(GitCommit)
            .where(GitCommit.project_id == project_id, GitCommit.sha.like(prefix + "%"))
            .limit(limit)
        )
        return list((await self._session.execute(q)).scalars())

    async def children_of(self, *, project_id: str, sha: str, limit: int = 20) -> list[GitCommit]:
        q = (
            self._select(GitCommit)
            .where(GitCommit.project_id == project_id, GitCommit.parents.contains([sha]))
            .order_by(GitCommit.committed_at, GitCommit.id)
            .limit(limit)
        )
        return list((await self._session.execute(q)).scalars())

    async def recent(self, *, project_id: str, limit: int) -> list[GitCommit]:
        return await self.timeline(
            project_id=project_id, topic=None, since=None, until=None, before=None, limit=limit
        )

    async def lock_for_projection(self, commit_id: str) -> GitCommit | None:
        return (
            await self._session.execute(
                self._select(GitCommit)
                .where(
                    GitCommit.id == commit_id,
                    GitCommit.projection_status.in_(("pending", "failed")),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

    async def mark(
        self, *, commit_id: str, status: str, memory_id: str | None, note: str | None
    ) -> None:
        await self._session.execute(
            self._update(GitCommit)
            .where(GitCommit.id == commit_id)
            .values(
                projection_status=status,
                memory_id=memory_id,
                projection_error=note[:300] if note else None,
                projection_attempts=GitCommit.projection_attempts + 1,
            )
        )

    async def unprojected(self, *, limit: int, max_attempts: int) -> list[str]:
        rows = (
            await self._session.execute(
                self._select_columns(GitCommit, GitCommit.id)
                .where(
                    GitCommit.projection_status.in_(("pending", "failed")),
                    GitCommit.projection_attempts < max_attempts,
                )
                .order_by(GitCommit.committed_at)
                .limit(limit)
            )
        ).all()
        return [r[0] for r in rows]


class AgentSessionRepository(UserScopedRepository):
    async def by_key(self, session_key: str) -> AgentSession | None:
        return (
            await self._session.execute(
                self._select(AgentSession).where(AgentSession.session_key == session_key)
            )
        ).scalar_one_or_none()

    async def by_ids(self, session_ids: list[str]) -> dict[str, AgentSession]:
        if not session_ids:
            return {}
        rows = (
            await self._session.execute(
                self._select(AgentSession).where(AgentSession.id.in_(session_ids))
            )
        ).scalars()
        return {r.id: r for r in rows}

    async def get_or_create(
        self,
        *,
        session_id: str,
        project_id: str,
        session_key: str,
        agent_name: str | None,
        started_at: datetime,
        now: datetime,
    ) -> tuple[AgentSession, bool]:
        """Idempotent per (space, key): a racing create resolves through the unique
        constraint, never through a prior read."""
        inserted = (
            await self._session.execute(
                insert(AgentSession)
                .values(
                    id=session_id,
                    user_id=self._user_id,
                    project_id=project_id,
                    session_key=session_key,
                    agent_name=agent_name,
                    status="active",
                    started_at=started_at,
                    created_at=now,
                    last_event_at=started_at,
                    event_count=0,
                )
                .on_conflict_do_nothing(constraint="agent_sessions_key_unique")
                .returning(AgentSession.id)
            )
        ).scalar_one_or_none()
        row = await self.by_key(session_key)
        assert row is not None
        return row, inserted is not None

    async def recent(
        self, *, project_id: str, limit: int, before: tuple[datetime, str] | None = None
    ) -> list[AgentSession]:
        q = self._select(AgentSession).where(AgentSession.project_id == project_id)
        if before:
            at, rid = before
            q = q.where(
                or_(
                    AgentSession.last_event_at < at,
                    and_(AgentSession.last_event_at == at, AgentSession.id < rid),
                )
            )
        q = q.order_by(AgentSession.last_event_at.desc(), AgentSession.id.desc()).limit(limit)
        return list((await self._session.execute(q)).scalars())

    async def record_event(self, *, session_id: str, at: datetime) -> None:
        """Count an event and advance `last_event_at` to the newest VALID time seen
        (the agent's own clock), so "most recent session" means most recent work, not
        most recently received batch."""
        await self._session.execute(
            self._update(AgentSession)
            .where(AgentSession.id == session_id)
            .values(
                last_event_at=func.greatest(AgentSession.last_event_at, at),
                event_count=AgentSession.event_count + 1,
            )
        )

    async def end(
        self, *, session_id: str, ended_at: datetime, outcome: str, summary: str | None
    ) -> None:
        await self._session.execute(
            self._update(AgentSession)
            .where(AgentSession.id == session_id)
            .values(status="ended", ended_at=ended_at, outcome=outcome, summary=summary)
        )


class AgentEventRepository(UserScopedRepository):
    """Reads, writes and projection bookkeeping for `agent_events`."""

    async def get_many_with_session_key(
        self, *, event_ids: list[str]
    ) -> dict[str, tuple[AgentEvent, str, str | None]]:
        """event id -> (event, session_key, agent_name)."""
        if not event_ids:
            return {}
        rows = (
            await self._session.execute(
                self._select(AgentEvent)
                .where(AgentEvent.id.in_(event_ids))
                .add_columns(AgentSession.session_key, AgentSession.agent_name)
                .join(AgentSession, AgentSession.id == AgentEvent.session_id)
            )
        ).all()
        return {r[0].id: (r[0], r[1], r[2]) for r in rows}

    async def insert_idempotent(
        self,
        *,
        event_id: str,
        project_id: str,
        session_id: str,
        event_type: str,
        event_key: str,
        occurred_at: datetime,
        recorded_at: datetime,
        payload: dict,
        payload_sha256: str,
        source: str | None,
    ) -> tuple[AgentEvent, bool]:
        """Insert, or return the event already recorded under this (session, key).
        The unique constraint decides; the second return value says which."""
        inserted = (
            await self._session.execute(
                insert(AgentEvent)
                .values(
                    id=event_id,
                    user_id=self._user_id,
                    project_id=project_id,
                    session_id=session_id,
                    event_type=event_type,
                    event_key=event_key,
                    occurred_at=occurred_at,
                    recorded_at=recorded_at,
                    payload=payload,
                    payload_sha256=payload_sha256,
                    source=source,
                    projection_status="pending",
                )
                .on_conflict_do_nothing(constraint="agent_events_idempotency")
                .returning(AgentEvent.id)
            )
        ).scalar_one_or_none()
        row = (
            await self._session.execute(
                self._select(AgentEvent).where(
                    AgentEvent.session_id == session_id, AgentEvent.event_key == event_key
                )
            )
        ).scalar_one()
        return row, inserted is not None

    async def insert_idempotent_lookup(self, session_id: str, event_key: str) -> AgentEvent | None:
        return (
            await self._session.execute(
                self._select(AgentEvent).where(
                    AgentEvent.session_id == session_id, AgentEvent.event_key == event_key
                )
            )
        ).scalar_one_or_none()

    async def count_for_session(self, session_id: str) -> int:
        return int(
            (
                await self._session.execute(
                    self._select_columns(AgentEvent, func.count()).where(
                        AgentEvent.session_id == session_id
                    )
                )
            ).scalar_one()
        )

    async def lock_for_projection(self, event_id: str) -> AgentEvent | None:
        """The event, row-locked, iff it still needs projecting. SKIP LOCKED: a
        concurrent projector of the same event gets None and does nothing."""
        return (
            await self._session.execute(
                self._select(AgentEvent)
                .where(
                    AgentEvent.id == event_id,
                    AgentEvent.projection_status.in_(("pending", "failed")),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

    async def get(self, event_id: str) -> AgentEvent | None:
        return (
            await self._session.execute(self._select(AgentEvent).where(AgentEvent.id == event_id))
        ).scalar_one_or_none()

    async def mark(
        self,
        *,
        event_id: str,
        status: str,
        memory_id: str | None,
        note: str | None,
        now: datetime,
        attempt: bool,
    ) -> None:
        values: dict = {
            "projection_status": status,
            "memory_id": memory_id,
            "projection_error": (note or None) and note[:300],
        }
        if status in ("projected", "skipped"):
            values["projected_at"] = now
        if attempt:
            values["projection_attempts"] = AgentEvent.projection_attempts + 1
        await self._session.execute(
            self._update(AgentEvent).where(AgentEvent.id == event_id).values(**values)
        )

    async def timeline(
        self,
        *,
        project_id: str,
        topic: str | None,
        types: list[str] | None,
        session_id: str | None,
        since: datetime | None,
        until: datetime | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> list[tuple[AgentEvent, str]]:
        """Newest-first by VALID time (`occurred_at`), id as the tie-break. Returns
        (event, session_key)."""
        q = (
            self._select(AgentEvent)
            .add_columns(AgentSession.session_key)
            .join(AgentSession, AgentSession.id == AgentEvent.session_id)
            .where(AgentEvent.project_id == project_id)
        )
        if types:
            q = q.where(AgentEvent.event_type.in_(types))
        if session_id:
            q = q.where(AgentEvent.session_id == session_id)
        if topic:
            q = q.where(cast(AgentEvent.payload, Text).ilike(like_pattern(topic), escape="\\"))
        if since:
            q = q.where(AgentEvent.occurred_at >= since)
        if until:
            q = q.where(AgentEvent.occurred_at < until)
        if before:
            at, rid = before
            q = q.where(
                or_(
                    AgentEvent.occurred_at < at,
                    and_(AgentEvent.occurred_at == at, AgentEvent.id < rid),
                )
            )
        q = q.order_by(AgentEvent.occurred_at.desc(), AgentEvent.id.desc()).limit(limit)
        return [(r[0], r[1]) for r in (await self._session.execute(q)).all()]

    async def touching_file(
        self, *, project_id: str, path: str, limit: int
    ) -> list[tuple[AgentEvent, str]]:
        """Events whose payload names this file as `path`, `old_path` or in `files`."""
        q = (
            self._select(AgentEvent)
            .add_columns(AgentSession.session_key)
            .join(AgentSession, AgentSession.id == AgentEvent.session_id)
            .where(
                AgentEvent.project_id == project_id,
                or_(
                    AgentEvent.payload.contains({"path": path}),
                    AgentEvent.payload.contains({"old_path": path}),
                    AgentEvent.payload["files"].contains([path]),
                ),
            )
            .order_by(AgentEvent.occurred_at.desc(), AgentEvent.id.desc())
            .limit(limit)
        )
        return [(r[0], r[1]) for r in (await self._session.execute(q)).all()]

    async def of_session(self, *, session_id: str, after_seq: int, limit: int) -> list[AgentEvent]:
        q = (
            self._select(AgentEvent)
            .where(AgentEvent.session_id == session_id, AgentEvent.recorded_seq > after_seq)
            .order_by(AgentEvent.recorded_seq)
            .limit(limit)
        )
        return list((await self._session.execute(q)).scalars())

    async def counts_by_type(self, *, session_id: str) -> dict[str, int]:
        rows = (
            await self._session.execute(
                self._select_columns(AgentEvent, AgentEvent.event_type, func.count())
                .where(AgentEvent.session_id == session_id)
                .group_by(AgentEvent.event_type)
            )
        ).all()
        return {r[0]: int(r[1]) for r in rows}

    async def by_id(self, event_id: str) -> tuple[AgentEvent, str] | None:
        row = (
            await self._session.execute(
                self._select(AgentEvent)
                .add_columns(AgentSession.session_key)
                .join(AgentSession, AgentSession.id == AgentEvent.session_id)
                .where(AgentEvent.id == event_id)
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def unprojected(self, *, limit: int, max_attempts: int) -> list[str]:
        rows = (
            await self._session.execute(
                self._select_columns(AgentEvent, AgentEvent.id)
                .where(
                    AgentEvent.projection_status.in_(("pending", "failed")),
                    AgentEvent.projection_attempts < max_attempts,
                )
                .order_by(AgentEvent.recorded_seq)
                .limit(limit)
            )
        ).all()
        return [r[0] for r in rows]

    async def unprojected_before(
        self, *, session_id: str, recorded_seq: int, limit: int
    ) -> list[str]:
        """Earlier events of this session that still need projecting, oldest first."""
        rows = (
            await self._session.execute(
                self._select_columns(AgentEvent, AgentEvent.id)
                .where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.recorded_seq < recorded_seq,
                    AgentEvent.projection_status.in_(("pending", "failed")),
                )
                .order_by(AgentEvent.recorded_seq)
                .limit(limit)
            )
        ).all()
        return [r[0] for r in rows]

    async def decisions_titled(
        self, *, project_id: str, title: str, before_seq: int
    ) -> list[AgentEvent]:
        """Earlier DECISION events with this title (case-insensitive), newest first."""
        rows = (
            await self._session.execute(
                self._select(AgentEvent)
                .where(
                    AgentEvent.project_id == project_id,
                    AgentEvent.event_type == "decision",
                    AgentEvent.recorded_seq < before_seq,
                    func.lower(AgentEvent.payload["title"].astext) == title.lower(),
                )
                .order_by(AgentEvent.recorded_seq.desc())
                .limit(5)
            )
        ).scalars()
        return list(rows)
