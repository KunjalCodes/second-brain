"""Health endpoints.

Two distinct checks, because managed hosts treat them differently:

* `/health`       liveness -- "is the process running?". Never touches a
                  database, so an outage in either store does not make the
                  platform kill and restart the container.
* `/health/ready` readiness -- "can this instance serve normal requests now?".
                  Returns 503 when Neo4j or PostgreSQL is unreachable, or when
                  the database schema is not at the revision this code expects,
                  so a deploy is not routed traffic it cannot serve. Every check
                  always runs and is reported, so an operator sees the full
                  picture in one request.

Readiness deliberately does **not** check the LLM provider. Extraction is an
asynchronous enhancement, never a request dependency (docs/adr/0008), so a
provider outage must not take the API out of rotation.

Both endpoints are unauthenticated, so their bodies carry **status codes
only** -- never exception text, hostnames, driver messages or configuration
names (Phase 11, M4). Details go to the server log.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from app.api.deps import get_app_settings, get_client, get_db
from app.core.config import Settings
from app.db.engine import Database
from app.graph.connection import GraphClient

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

APP_VERSION = "0.11.0"
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
_MIGRATION_CACHE_SECONDS = 60.0
_migration_cache: tuple[float, str] | None = None


def expected_migration_head() -> str | None:
    """The Alembic head revision this code ships with (None if unreadable)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception:  # noqa: BLE001 - reported as a readiness failure, logged
        logger.exception("health_migration_head_unreadable")
        return None
    return heads[0] if len(heads) == 1 else None


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": APP_VERSION}


async def _check_neo4j(client: GraphClient, settings: Settings) -> str:
    if not settings.neo4j_configured:
        return "not_configured"
    try:
        if not client.is_connected:
            await client.connect()
        await client.verify()
    except Exception as exc:  # noqa: BLE001 - reported as a code, logged in full
        logger.warning("health_neo4j_unavailable reason=%s", type(exc).__name__)
        return "unavailable"
    return "ok"


async def _check_database(db: Database, settings: Settings) -> str:
    if not settings.database_configured:
        return "not_configured"
    try:
        if not db.is_connected:
            await db.connect()
        await db.verify()
    except Exception as exc:  # noqa: BLE001
        logger.warning("health_postgres_unavailable reason=%s", type(exc).__name__)
        return "unavailable"
    return "ok"


async def _check_migrations(db: Database) -> str:
    """`ok` when the database is at exactly the head revision this code
    expects; `mismatch` otherwise (behind, ahead, or diverged). Cached briefly
    so frequent health probes do not query `alembic_version` every time."""
    global _migration_cache
    now = time.monotonic()
    if _migration_cache is not None and now - _migration_cache[0] < _MIGRATION_CACHE_SECONDS:
        return _migration_cache[1]
    expected = expected_migration_head()
    if expected is None:
        result = "unknown"
    else:
        try:
            async with db.session() as session:
                current = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001
            logger.warning("health_migration_state_unreadable reason=%s", type(exc).__name__)
            return "unavailable"  # not cached: retried on the next probe
        result = "ok" if current == expected else "mismatch"
        if result == "mismatch":
            logger.error("health_migration_mismatch expected=%s current=%s", expected, current)
    _migration_cache = (now, result)
    return result


def reset_migration_cache() -> None:
    """Tests only."""
    global _migration_cache
    _migration_cache = None


@router.get("/health/ready")
async def readiness(
    response: Response,
    client: Annotated[GraphClient, Depends(get_client)],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    """Report dependency health without raising, so the body always says which
    dependency is the problem -- as a code, never as internal detail."""
    checks = {
        "neo4j": await _check_neo4j(client, settings),
        "postgresql": await _check_database(db, settings),
    }
    checks["migrations"] = (
        await _check_migrations(db) if checks["postgresql"] == "ok" else "unavailable"
    )
    if any(value != "ok" for value in checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "checks": checks}
    return {"status": "ready", "checks": checks}
