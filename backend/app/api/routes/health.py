"""Health endpoints.

Two distinct checks, because managed hosts treat them differently:

* `/health`       liveness -- process is up. Never touches a database, so an outage
                  in either store does not make the platform kill and restart the
                  container.
* `/health/ready` readiness -- dependencies actually answer. Returns 503 when Neo4j
                  or PostgreSQL is unreachable, so a deploy is not routed traffic it
                  cannot serve. Both checks always run and are both reported, even
                  if one already failed, so an operator sees the full picture in one
                  request rather than fixing one outage at a time.

Readiness deliberately does **not** check the LLM provider. Extraction is an
asynchronous enhancement, never a request dependency (docs/adr/0008), so a Gemini
outage must not take the API out of rotation.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_app_settings, get_client, get_db
from app.core.config import Settings
from app.core.errors import AppError
from app.db.engine import Database
from app.graph.connection import GraphClient

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: Annotated[Settings, Depends(get_app_settings)]) -> dict[str, Any]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "phase": 2,
    }


async def _check_neo4j(client: GraphClient, settings: Settings) -> dict[str, Any]:
    if not settings.neo4j_configured:
        return {
            "status": "not_configured",
            "detail": "NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD are not all set",
        }
    try:
        await client.verify()
    except AppError as exc:
        return {"status": "error", "detail": exc.message}
    return {"status": "ok"}


async def _check_database(db: Database, settings: Settings) -> dict[str, Any]:
    if not settings.database_configured:
        return {"status": "not_configured", "detail": "DATABASE_URL is not set"}
    try:
        if not db.is_connected:
            await db.connect()
        await db.verify()
    except AppError as exc:
        return {"status": "error", "detail": exc.message}
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    response: Response,
    client: Annotated[GraphClient, Depends(get_client)],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    """Report dependency health without raising, so the body always explains why."""
    neo4j_check = await _check_neo4j(client, settings)
    database_check = await _check_database(db, settings)

    checks: dict[str, Any] = {"neo4j": neo4j_check, "postgresql": database_check}
    all_ok = neo4j_check["status"] == "ok" and database_check["status"] == "ok"

    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "checks": checks}
    return {"status": "ready", "checks": checks}
