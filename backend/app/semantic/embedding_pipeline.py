"""The embedding lifecycle: embed-after-create, and the bounded backfill sweep.

Mirrors `app/extraction/runner.py`'s own asynchronous, at-least-once shape (ADR
0009, PHASE3_PLAN.md sections 3/13): embedding a memory happens *after* it is
already durably persisted in Neo4j, never as a precondition for creating it, and
a failure here is never allowed to affect the memory itself -- only whether it is
findable by semantic/vector search yet.

## Consistency model (ADR 0009)

    Neo4j Memory.id  (created first, always)
            |
            v
    PostgreSQL memory_embeddings.memory_id  (created after, asynchronously,
                                              at-least-once)

A `memory_embeddings` row can lag or be briefly missing; it must never be able to
imply a memory does not exist. `embed_and_store_memory` never raises -- it logs
and returns `False` on any embedding-provider or PostgreSQL failure, exactly the
"embedding failure must never prevent memory creation" invariant.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import AppError
from app.db.engine import Database
from app.embeddings.base import EmbeddingClient
from app.graph.connection import GraphClient
from app.repositories.embedding_repository import EmbeddingRepository
from app.repositories.memory_repository import MemoryRepository

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def embed_and_store_memory(
    *,
    user_id: str,
    memory_id: str,
    content: str,
    db: Database,
    embedding_client: EmbeddingClient,
    settings: Settings,
    now_fn: Callable[[], datetime] = _utcnow,
    id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> bool:
    """Embed one memory's content and store the vector. Never raises.

    Returns `True` if a new row was stored, `False` for any failure (embedding
    provider unavailable/misconfigured, or a PostgreSQL error) or if a row for
    this `(memory_id, provider, model)` already existed. Callers -- the extraction
    runner, the direct memory-creation route's background task, and the backfill
    sweep -- all treat `False` identically: log it, move on, never propagate.
    """
    try:
        result = await embedding_client.embed(
            text=content, timeout=settings.embedding_timeout_seconds
        )
    except AppError as exc:
        logger.warning(
            "Embedding failed for memory %s (memory remains valid and usable): %s",
            memory_id,
            exc.message,
        )
        return False

    try:
        async with db.session() as session:
            repo = EmbeddingRepository(session, user_id=user_id)
            return await repo.upsert(
                embedding_id=id_fn(),
                memory_id=memory_id,
                content=content,
                embedding=result.vector,
                embedding_provider=result.provider,
                embedding_model=result.model,
                embedding_dimension=result.dimension,
                now=now_fn(),
            )
    except AppError as exc:
        logger.warning(
            "Storing the embedding for memory %s failed (memory remains valid and "
            "usable; the backfill sweep will retry): %s",
            memory_id,
            exc.message,
        )
        return False


async def run_embedding_backfill_tick(
    *,
    db: Database,
    graph_client: GraphClient,
    embedding_client: EmbeddingClient,
    settings: Settings,
    user_id: str,
    batch_size: int | None = None,
    now_fn: Callable[[], datetime] = _utcnow,
) -> dict[str, int]:
    """One bounded batch: find this user's memories with no embedding row for the
    *current* `(provider, model)`, embed them, store them.

    Resumable (progress is `memory_embeddings` itself -- a killed run simply
    leaves fewer rows than target, and the next run re-queries exactly the same
    gap), idempotent (`EmbeddingRepository.upsert`'s `ON CONFLICT DO NOTHING`),
    bounded (`batch_size`), user-scoped (every read and write here carries
    `user_id`), and observable (the returned counts).

    The source of truth for "which memories exist" is Neo4j
    (PHASE3_PLAN.md section 13) -- this reads a bounded page of memory ids and
    content from there, then anti-joins against `memory_embeddings` in this
    driver rather than via a cross-database JOIN, which does not exist.
    """
    target = batch_size or settings.embedding_backfill_batch_size
    # Scan a wider window than the embed budget: most scanned memories are
    # expected to already be embedded (a live embed already ran for them), so a
    # 1:1 scan:embed ratio would make almost no forward progress once the
    # backlog is mostly caught up. Bounded all the same -- never unbounded.
    scan_limit = target * 4
    rows = await MemoryRepository(graph_client).list_for_backfill(
        user_id=user_id, limit=scan_limit
    )

    scanned = 0
    embedded = 0
    skipped_already_present = 0
    failed = 0
    for row in rows:
        if embedded + failed >= target:
            break  # per-tick embed/failure budget reached; resume next tick
        scanned += 1
        memory_id = row["id"]
        content = row["content"]
        async with db.session() as session:
            already = await EmbeddingRepository(session, user_id=user_id).exists(
                memory_id=memory_id,
                embedding_provider=settings.embedding_provider,
                embedding_model=settings.embedding_model_resolved,
            )
        if already:
            skipped_already_present += 1
            continue

        stored = await embed_and_store_memory(
            user_id=user_id,
            memory_id=memory_id,
            content=content,
            db=db,
            embedding_client=embedding_client,
            settings=settings,
            now_fn=now_fn,
        )
        if stored:
            embedded += 1
        else:
            failed += 1

    return {
        "scanned": scanned,
        "embedded": embedded,
        "skipped_already_present": skipped_already_present,
        "failed": failed,
    }
