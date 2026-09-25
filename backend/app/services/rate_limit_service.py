"""PostgreSQL fixed-window rate limiting (Phase 9, docs/PHASE9_PLAN.md
section 11, ADR 0023).

Every hit runs in its OWN short transaction, committed before the protected
request continues. The request-scoped session (`Database.session()`) rolls
back on any `AppError`, so a counter written inside it would be erased by the
very 401/429 it produced -- a failed login would never count (PHASE9_PLAN.md
F12). This is pinned by an integration test.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.core.config import Settings
from app.core.errors import RateLimitedError
from app.repositories.rate_limit_repository import RateLimitRepository


class LimitClass(StrEnum):
    AUTH_LOGIN_IP = "auth_login_ip"
    AUTH_LOGIN_EMAIL = "auth_login_email"
    AUTH_REGISTER = "auth_register"
    GRAPH = "graph"
    SEARCH = "search"
    AGENT = "agent"
    MESSAGE = "message"
    INGEST = "ingest"
    WRITE = "write"


#: (settings field holding the limit, window in seconds). Windows are fixed
#: per class; the limits are configurable (PHASE9_PLAN.md section 11 table).
LIMITS: dict[LimitClass, tuple[str, int]] = {
    LimitClass.AUTH_LOGIN_IP: ("rate_limit_login_per_ip", 15 * 60),
    LimitClass.AUTH_LOGIN_EMAIL: ("rate_limit_login_per_email", 15 * 60),
    LimitClass.AUTH_REGISTER: ("rate_limit_register_per_ip", 60 * 60),
    LimitClass.GRAPH: ("rate_limit_graph_per_user", 60),
    LimitClass.SEARCH: ("rate_limit_search_per_user", 60),
    LimitClass.AGENT: ("rate_limit_agent_per_user", 60 * 60),
    LimitClass.MESSAGE: ("rate_limit_message_per_user", 60 * 60),
    LimitClass.INGEST: ("rate_limit_ingest_per_user", 60 * 60),
    LimitClass.WRITE: ("rate_limit_write_per_user", 60),
}

#: Probability that one hit also deletes a bounded batch of expired windows.
CLEANUP_PROBABILITY = 0.01
_LONGEST_WINDOW = max(w for _, w in LIMITS.values())


@dataclass(frozen=True)
class Decision:
    count: int
    limit: int
    retry_after_seconds: int

    @property
    def allowed(self) -> bool:
        return self.count <= self.limit


def window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=UTC)


def retry_after(now: datetime, window_seconds: int) -> int:
    end = window_start(now, window_seconds) + timedelta(seconds=window_seconds)
    return max(1, int((end - now).total_seconds() + 0.999))


class RateLimiter:
    def __init__(
        self,
        db,  # noqa: ANN001 - app.db.engine.Database
        settings: Settings,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._db = db
        self._settings = settings
        self._now = now_fn
        self._rng = rng

    async def hit(self, limit_class: LimitClass, identity: str) -> Decision | None:
        """Count one attempt; None when rate limiting is disabled. Commits
        before returning, whatever the caller does next."""
        if not self._settings.rate_limit_enabled:
            return None
        field, window = LIMITS[limit_class]
        limit = int(getattr(self._settings, field))
        now = self._now()
        async with self._db.session() as session:
            repo = RateLimitRepository(session)
            count = await repo.increment(
                bucket_key=f"{limit_class.value}:{identity}",
                window_start=window_start(now, window),
            )
            if self._rng() < CLEANUP_PROBABILITY:
                await repo.delete_expired(before=now - timedelta(seconds=2 * _LONGEST_WINDOW))
        return Decision(count=count, limit=limit, retry_after_seconds=retry_after(now, window))

    async def enforce(self, limit_class: LimitClass, identity: str) -> None:
        decision = await self.hit(limit_class, identity)
        if decision is not None and not decision.allowed:
            raise RateLimitedError(
                limit_class=limit_class.value,
                retry_after_seconds=decision.retry_after_seconds,
            )
