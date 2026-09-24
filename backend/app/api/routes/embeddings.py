"""The internal embedding backfill trigger (ADR 0009, PHASE3_PLAN.md section 13).

Not user-scoped and not part of the public API surface -- guarded by
`X-Internal-Token` (`InternalTokenDep`), the same as `POST /internal/extraction/sweep`.
Intended to be called by the managed host's own cron.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import EmbeddingClientDep, InternalTokenDep, SettingsDep
from app.db.engine import get_database
from app.graph.connection import get_graph_client
from app.repositories.user_repository import UserRepository
from app.schemas.embedding import EmbeddingBackfillResult
from app.semantic.embedding_pipeline import run_embedding_backfill_tick

internal_router = APIRouter(prefix="/internal/embeddings", tags=["internal"])

#: Bounded page of users processed per call -- a large deployment would need
#: several calls (or a smaller max_users per call, more frequently) to fully
#: sweep everyone, matching the same "bounded, resumable, called repeatedly"
#: shape as the extraction sweep rather than attempting an unbounded pass.
_DEFAULT_MAX_USERS = 100


@internal_router.post(
    "/backfill",
    response_model=EmbeddingBackfillResult,
    summary="Run one bounded embedding backfill pass (internal; requires X-Internal-Token)",
)
async def trigger_backfill(
    _: InternalTokenDep,
    embedding_client: EmbeddingClientDep,
    settings: SettingsDep,
    max_users: Annotated[int, Query(ge=1, le=1000)] = _DEFAULT_MAX_USERS,
) -> EmbeddingBackfillResult:
    """Find memories with no embedding row under the currently-configured
    `(EMBEDDING_PROVIDER, EMBEDDING_MODEL)` and embed them, for a bounded page of
    users. Neo4j's `$user_id` scoping means there is no single "all users" query
    (unlike the extraction sweep's PostgreSQL-only claim step) -- this loops over
    a bounded page of known users instead, running one bounded, idempotent,
    resumable tick per user (`run_embedding_backfill_tick`).
    """
    db = get_database()
    graph = get_graph_client()

    async with db.session() as session:
        user_ids = await UserRepository(session).list_ids(limit=max_users)

    totals = {"scanned": 0, "embedded": 0, "skipped_already_present": 0, "failed": 0}
    for user_id in user_ids:
        counts = await run_embedding_backfill_tick(
            db=db,
            graph_client=graph,
            embedding_client=embedding_client,
            settings=settings,
            user_id=user_id,
        )
        for key in totals:
            totals[key] += counts[key]

    return EmbeddingBackfillResult(users_processed=len(user_ids), **totals)
