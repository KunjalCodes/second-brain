"""Phase 11 (M7): migrations against real, throwaway PostgreSQL databases.

* fresh database -> `alembic upgrade head` -> expected schema, the pgvector
  HNSW index present, and `alembic check` reports no drift (so autogenerate
  can never propose dropping that index again);
* representative existing database (at the Phase 7 revision, with data) ->
  upgrade -> data preserved and the Phase 9 schema present.

Each test creates its own `ai_second_brain_migtest_*` database and drops it
afterwards; the shared test database is never touched. Skipped when the role
cannot create databases.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.scratch_db]

BACKEND = Path(__file__).resolve().parents[2]
PHASE7 = "ec358e32d566"


def _server_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://localhost/ai_second_brain_test")
    return url.replace("+asyncpg", "").rsplit("/", 1)[0]


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(f"{_server_url()}/postgres")
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
async def scratch_db() -> Iterator[str]:
    name = f"ai_second_brain_migtest_{uuid.uuid4().hex[:10]}"
    try:
        await _admin(f'CREATE DATABASE "{name}"')
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create a scratch database: {type(exc).__name__}")
    try:
        yield name
    finally:
        await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _alembic(db: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": f"{_server_url().replace('postgresql://', 'postgresql+asyncpg://')}/{db}",
    }
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


async def _fetch(db: str, sql: str, *params: object) -> list:
    conn = await asyncpg.connect(f"{_server_url()}/{db}")
    try:
        return await conn.fetch(sql, *params)
    finally:
        await conn.close()


async def test_fresh_database_upgrades_to_the_expected_schema(scratch_db: str) -> None:
    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]

    tables = {
        r["tablename"]
        for r in await _fetch(
            scratch_db, "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )
    }
    assert {
        "users",
        "conversations",
        "messages",
        "extraction_jobs",
        "memory_embeddings",
        "documents",
        "document_chunks",
        "ingestion_jobs",
        "evaluation_runs",
        "user_accounts",
        "auth_sessions",
        "rate_limit_counters",
        "alembic_version",
    } <= tables
    hnsw = await _fetch(
        scratch_db,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'memory_embeddings_hnsw_cosine'",
    )
    assert hnsw and "hnsw" in hnsw[0]["indexdef"]

    check = _alembic(scratch_db, "check")
    assert check.returncode == 0, (check.stdout + check.stderr)[-2000:]
    assert "No new upgrade operations detected" in check.stdout + check.stderr


async def test_existing_database_upgrades_with_data_preserved(scratch_db: str) -> None:
    assert _alembic(scratch_db, "upgrade", PHASE7).returncode == 0
    conn = await asyncpg.connect(f"{_server_url()}/{scratch_db}")
    try:
        await conn.execute("INSERT INTO users (id, created_at) VALUES ('legacy-user', now())")
        await conn.execute(
            "INSERT INTO conversations (id, user_id, created_at, updated_at) "
            "VALUES ($1, 'legacy-user', now(), now())",
            uuid.UUID("00000000-0000-0000-0000-000000000001"),
        )
        await conn.execute(
            "INSERT INTO memory_embeddings (id, user_id, memory_id, embedding_provider, "
            "embedding_model, embedding_dimension, embedding, content_hash, created_at) "
            "VALUES ($1, 'legacy-user', 'm-1', 'fake', 'fake-v1', 768, $2::vector, 'h', now())",
            uuid.UUID("00000000-0000-0000-0000-000000000002"),
            "[" + ",".join(["0.1"] * 768) + "]",
        )
    finally:
        await conn.close()

    up = _alembic(scratch_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]
    assert [
        r["version_num"]
        for r in await _fetch(scratch_db, "SELECT version_num FROM alembic_version")
    ] == ["320f6d898a92"]
    assert (await _fetch(scratch_db, "SELECT count(*) AS n FROM users WHERE id='legacy-user'"))[0][
        "n"
    ] == 1
    assert (await _fetch(scratch_db, "SELECT count(*) AS n FROM memory_embeddings"))[0]["n"] == 1
    assert (await _fetch(scratch_db, "SELECT count(*) AS n FROM user_accounts"))[0]["n"] == 0
    assert await _fetch(
        scratch_db, "SELECT 1 FROM pg_indexes WHERE indexname = 'memory_embeddings_hnsw_cosine'"
    )
