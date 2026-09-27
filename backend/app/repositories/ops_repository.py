"""Operational gauges for `GET /internal/metrics` (Phase 11, M5).

**Cross-user by design and aggregate-only.** Every query returns COUNTS or
ages -- never a row, an id, a user id or any content -- which is why this
module is exempt from the user-scoping import ban in
`tests/unit/test_sql_safety.py` (see the exemption note there).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, Document, ExtractionJob, IngestionJob

_NON_TERMINAL_DOCUMENT = ("uploaded", "validating", "parsing", "chunking", "chunked", "extracting")


async def queue_gauges(session: AsyncSession, *, now: datetime) -> dict[str, object]:
    gauges: dict[str, object] = {}
    for name, model in (("extraction", ExtractionJob), ("ingestion", IngestionJob)):
        by_status = {
            status: count
            for status, count in (
                await session.execute(select(model.status, func.count()).group_by(model.status))
            ).all()
        }
        oldest_pending = (
            await session.execute(
                select(func.min(model.created_at)).where(model.status == "pending")
            )
        ).scalar()
        abandoned_24h = (
            await session.execute(
                select(func.count()).where(
                    model.status == "abandoned", model.completed_at >= now - timedelta(days=1)
                )
            )
        ).scalar()
        retries = (
            await session.execute(
                select(func.coalesce(func.sum(model.attempts), 0)).where(
                    model.status.in_(("pending", "in_progress", "failed"))
                )
            )
        ).scalar()
        gauges[name] = {
            "by_status": by_status,
            "depth": by_status.get("pending", 0) + by_status.get("failed", 0),
            "in_progress": by_status.get("in_progress", 0),
            "oldest_pending_age_seconds": (
                round((now - oldest_pending).total_seconds()) if oldest_pending else None
            ),
            "abandoned_last_24h": abandoned_24h,
            "attempts_on_open_jobs": retries,
        }
    gauges["documents_stuck_over_30min"] = (
        await session.execute(
            select(func.count()).where(
                Document.status.in_(_NON_TERMINAL_DOCUMENT),
                Document.uploaded_at < now - timedelta(minutes=30),
            )
        )
    ).scalar()
    gauges["active_sessions"] = (
        await session.execute(
            select(func.count()).where(
                AuthSession.revoked_at.is_(None), AuthSession.expires_at > now
            )
        )
    ).scalar()
    return gauges
