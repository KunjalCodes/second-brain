"""Fixed-window rate-limit counters (Phase 9, ADR 0023).

Exempt from the `UserScopedRepository` rule: the table holds no user-owned
data -- only `<class>:<identity>` keys (user ids, client IPs, or SHA-256 email
digests) and counts -- and one limiter must count across every caller.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RateLimitCounter

#: Bound on rows removed by one opportunistic cleanup.
CLEANUP_BATCH = 1000


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def increment(self, *, bucket_key: str, window_start: datetime) -> int:
        """Atomic upsert; returns the count INCLUDING this hit."""
        stmt = (
            insert(RateLimitCounter)
            .values(bucket_key=bucket_key, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=["bucket_key", "window_start"],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def delete_expired(self, *, before: datetime) -> None:
        stale = (
            select(RateLimitCounter.bucket_key, RateLimitCounter.window_start)
            .where(RateLimitCounter.window_start < before)
            .limit(CLEANUP_BATCH)
        )
        await self._s.execute(
            delete(RateLimitCounter).where(
                RateLimitCounter.window_start < before,
                RateLimitCounter.bucket_key.in_(
                    stale.with_only_columns(RateLimitCounter.bucket_key)
                ),
            )
        )
