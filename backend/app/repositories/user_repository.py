"""Identity-reference persistence.

`users` is deliberately not scoped by `UserScopedRepository`: a user row's `id` *is*
the isolation key everything else filters by, so there is no separate `user_id`
column to scope against. This is the one repository that legitimately has no
per-call user filter -- exempted from `tests/unit/test_sql_safety.py`'s AST audit
for the same reason `app/repositories/extraction_queue.py` is: `list_ids` (Phase 3)
is a deliberate, bounded cross-user enumeration used only by the embedding backfill
sweep's internal endpoint (`POST /internal/embeddings/backfill`), which -- like the
extraction sweep -- must walk every user's data by design, not by accident.

**This table is not an authentication system.** It stores no passwords, no API
keys, no credentials, no sessions and no sensitive profile data -- see
`app/db/models.py::User` and docs/adr/0005 (authentication is Phase 9).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User as UserRow


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_exists(self, user_id: str, *, now: datetime) -> None:
        """Idempotently create the identity reference row.

        Every user-owned table has a foreign key to `users.id`, so this must run
        before the first write for a given user id. `ON CONFLICT DO NOTHING` makes
        repeated calls (the common case -- most requests are from an existing user)
        a no-op rather than a round trip that errors and is caught.
        """
        stmt = (
            insert(UserRow)
            .values(id=user_id, created_at=now)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await self._session.execute(stmt)

    async def list_ids(self, *, limit: int) -> list[str]:
        """A bounded page of every known user id, oldest first.

        Cross-user by design: the embedding backfill sweep (Phase 3) needs to run
        `run_embedding_backfill_tick` once per user, since Neo4j's `$user_id`
        scoping discipline means there is no "all users at once" Cypher query --
        this is how the sweep's internal endpoint discovers which users to loop
        over.
        """
        stmt = select(UserRow.id).order_by(UserRow.created_at).limit(limit)
        result = await self._session.execute(stmt)
        return [row[0] for row in result]
