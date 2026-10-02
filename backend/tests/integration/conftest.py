"""Integration test fixtures.

These tests run against real Neo4j and/or real PostgreSQL (local or managed). They
are the only coverage of the actual query text (Cypher and SQL alike), so they are
not optional -- but each store's fixtures skip automatically when that store is
unreachable, so `pytest` stays runnable on a machine with only one, or neither,
installed.

Run with both available:

    NEO4J_URI=neo4j+s://<id>.databases.neo4j.io NEO4J_USERNAME=neo4j NEO4J_PASSWORD=<pw> \
    DATABASE_URL=postgresql+asyncpg://user:pw@host/db \
    uv run pytest -m integration

The PostgreSQL schema must already be migrated (`uv run alembic upgrade head`)
before running these -- fixtures probe for the expected tables and skip with a
clear message if they are missing, rather than trying to run migrations themselves.

Each test gets unique user ids and its own cleanup, so tests neither collide nor
depend on execution order, and a failure leaves no residue for the next run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.engine import Database
from app.graph.connection import GraphClient
from app.graph.schema import SCHEMA_STATEMENTS
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService
from tests.conftest import JAN, FrozenClock

pytestmark = pytest.mark.integration

#: Deletes one test user's subgraph. Only integration tests may delete anything.
CLEANUP = "MATCH (n {user_id: $user_id}) DETACH DELETE n"

#: PostgreSQL cleanup, in FK-safe order: children before parents.
_PG_CLEANUP_STATEMENTS = (
    # memory_review_candidates and review_audit_log both FK to
    # extraction_candidates.id, so they must go first (PHASE4_PLAN.md section 6).
    "DELETE FROM review_audit_log WHERE user_id = :user_id",
    "DELETE FROM memory_review_candidates WHERE user_id = :user_id",
    "DELETE FROM extraction_candidates WHERE user_id = :user_id",
    "DELETE FROM extraction_jobs WHERE user_id = :user_id",
    "DELETE FROM entity_merge_candidates WHERE user_id = :user_id",
    "DELETE FROM memory_embeddings WHERE user_id = :user_id",
    # Phase 6: ingestion_jobs/document_chunks both FK to documents with
    # ON DELETE CASCADE, so deleting documents alone would suffice -- listed
    # explicitly anyway, in FK-safe order, to match this file's existing
    # style of never relying on cascade for test cleanup.
    "DELETE FROM ingestion_jobs WHERE user_id = :user_id",
    "DELETE FROM document_chunks WHERE user_id = :user_id",
    "DELETE FROM documents WHERE user_id = :user_id",
    "DELETE FROM messages WHERE user_id = :user_id",
    "DELETE FROM conversations WHERE user_id = :user_id",
    # Phase 9: auth rows reference users.id; counters are keyed by user id.
    "DELETE FROM auth_sessions WHERE user_id = :user_id",
    "DELETE FROM user_accounts WHERE user_id = :user_id",
    "DELETE FROM rate_limit_counters WHERE bucket_key LIKE '%:u:' || :user_id",
    # Phase 16: a project's tokens, then the project and its memory-space `users` row
    # (RETURNING makes the space id available after the project is gone).
    "DELETE FROM project_tokens WHERE project_id IN "
    "(SELECT id FROM projects WHERE owner_user_id = :user_id)",
    "WITH p AS (DELETE FROM projects WHERE owner_user_id = :user_id RETURNING space_id) "
    "DELETE FROM users WHERE id IN (SELECT space_id FROM p)",
    "DELETE FROM users WHERE id = :user_id",
)


async def _neo4j_probe() -> str | None:
    """Return None if Neo4j is reachable, otherwise the reason it is not."""
    settings = get_settings()
    if not settings.neo4j_configured:
        return "NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD are not all set"
    client = GraphClient(settings)
    try:
        await client.connect()
        await client.verify()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()
    return None


async def _postgres_probe() -> str | None:
    """Return None if PostgreSQL is reachable AND migrated, otherwise why not."""
    settings = get_settings()
    if not settings.database_configured:
        return "DATABASE_URL is not set"
    db = Database(settings)
    try:
        await db.connect()
        await db.verify()
        async with db.session() as session:
            await session.execute(text("SELECT 1 FROM users LIMIT 0"))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc} (has `alembic upgrade head` been run?)"
    finally:
        await db.close()
    return None


@pytest.fixture(scope="session")
async def neo4j_available() -> str | None:
    return await _neo4j_probe()


@pytest.fixture(scope="session")
async def postgres_available() -> str | None:
    return await _postgres_probe()


@pytest.fixture
async def graph(neo4j_available: str | None) -> AsyncIterator[GraphClient]:
    """A connected client with the schema applied, or a skip."""
    if neo4j_available is not None:
        pytest.skip(f"Neo4j not available ({neo4j_available})")
    client = GraphClient(get_settings())
    await client.connect()
    for statement in SCHEMA_STATEMENTS:
        await client.write(statement)
    yield client
    await client.close()


@pytest.fixture
async def db(postgres_available: str | None) -> AsyncIterator[Database]:
    """A connected PostgreSQL `Database`, or a skip."""
    if postgres_available is not None:
        pytest.skip(f"PostgreSQL not available ({postgres_available})")
    database = Database(get_settings())
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def db_session(db: Database) -> AsyncIterator[AsyncSession]:
    """One PostgreSQL session per test, commit-on-success like the real request path."""
    async with db.session() as session:
        yield session


@pytest.fixture
def user_id() -> str:
    """A unique namespace per test, so tests cannot interfere with each other."""
    return f"itest-{uuid.uuid4()}"


@pytest.fixture
def other_user_id() -> str:
    return f"itest-other-{uuid.uuid4()}"


@pytest.fixture
async def cleanup(graph: GraphClient, user_id: str, other_user_id: str) -> AsyncIterator[None]:
    yield
    for uid in (user_id, other_user_id):
        await graph.write(CLEANUP, {"user_id": uid})


@pytest.fixture
async def pg_cleanup(db: Database, user_id: str, other_user_id: str) -> AsyncIterator[None]:
    """PostgreSQL counterpart to `cleanup`. Separate fixture (rather than folded
    into `cleanup`) so Neo4j-only tests never need a PostgreSQL connection.
    """
    yield
    async with db.session() as session:
        for uid in (user_id, other_user_id):
            for statement in _PG_CLEANUP_STATEMENTS:
                await session.execute(text(statement), {"user_id": uid})


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(JAN)


@pytest.fixture
def memories(graph: GraphClient, clock: FrozenClock) -> MemoryService:
    return MemoryService(MemoryRepository(graph), now_fn=clock)


@pytest.fixture
def retrieval(graph: GraphClient, clock: FrozenClock) -> RetrievalService:
    return RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
