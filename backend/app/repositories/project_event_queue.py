"""Cross-space enumeration of agent events that still need projecting (Phase 16).

Exempt from `tests/unit/test_sql_safety.py`'s `UserScopedRepository` rule for the
same reason as `extraction_queue.py`: the sweeper must walk every space's backlog in
one pass, by design. It returns ONLY (space id, event id) pairs -- never content --
and the projection it feeds re-reads each event through the user-scoped repository.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEvent, GitCommit, Project


async def unprojected_events(
    session: AsyncSession, *, limit: int, max_attempts: int
) -> list[tuple[str, str]]:
    """Oldest-recorded first, so a session's events are projected in order."""
    rows = (
        await session.execute(
            select(AgentEvent.user_id, AgentEvent.id)
            .where(
                AgentEvent.projection_status.in_(("pending", "failed")),
                AgentEvent.projection_attempts < max_attempts,
            )
            .order_by(AgentEvent.recorded_seq)
            .limit(limit)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


async def unprojected_commits(
    session: AsyncSession, *, limit: int, max_attempts: int
) -> list[tuple[str, str, str]]:
    """(space id, commit id, project name), oldest commit first."""
    rows = (
        await session.execute(
            select(GitCommit.user_id, GitCommit.id, Project.name)
            .join(Project, Project.id == GitCommit.project_id)
            .where(
                GitCommit.projection_status.in_(("pending", "failed")),
                GitCommit.projection_attempts < max_attempts,
            )
            .order_by(GitCommit.committed_at)
            .limit(limit)
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]
