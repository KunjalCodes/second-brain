"""FastAPI application entry point.

Deployment notes
----------------
The importable entry point is ``app.main:app``, which is what every common managed
Python host expects (``uvicorn app.main:app --host 0.0.0.0 --port $PORT``). The
application:

* reads all configuration from environment variables (no config files, no secrets in code)
* never binds a port itself, so the platform's ``$PORT`` is authoritative
* keeps no state on the local filesystem -- canonical memory lives in Neo4j,
  conversation/extraction state lives in PostgreSQL (docs/adr/0006)
* opens the graph driver and the database engine on startup, closes both on shutdown
* stays alive when either is unreachable, reporting it through ``/health/ready``
  rather than crash-looping
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.cookies import clear_session_cookie
from app.api.routes import (
    agent,
    auth,
    conversations,
    documents,
    embeddings,
    entities,
    extraction,
    graph,
    health,
    memories,
    review,
    search,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.engine import get_database
from app.graph.connection import get_graph_client

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the graph driver on startup, close it on shutdown.

    A failure to reach the graph is logged at ERROR and surfaced by
    ``/health/ready``, but does not prevent startup: on a managed host, refusing to
    boot turns a transient database problem into a restart loop that is much harder
    to diagnose than an unhealthy-but-running service.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Starting %s (env=%s, phase=2)", settings.app_name, settings.app_env)

    client = get_graph_client()
    if settings.neo4j_configured:
        try:
            await client.connect()
            await client.verify()
            logger.info("Connected to Neo4j")
        except Exception:
            logger.exception(
                "Could not reach Neo4j at startup. The API will serve but "
                "/health/ready will report unavailable until it recovers."
            )
    else:
        logger.error(
            "Neo4j is not configured: set NEO4J_URI, NEO4J_USERNAME and "
            "NEO4J_PASSWORD. See .env.example."
        )

    # PostgreSQL holds conversations, messages and extraction state (docs/adr/0006).
    # Same startup contract as Neo4j: a failure is logged and surfaced by
    # /health/ready, but never prevents the process from starting.
    database = get_database()
    if settings.database_configured:
        try:
            await database.connect()
            await database.verify()
            logger.info("Connected to PostgreSQL")
        except Exception:
            logger.exception(
                "Could not reach PostgreSQL at startup. The API will serve but "
                "/health/ready will report unavailable until it recovers."
            )
    else:
        logger.error("PostgreSQL is not configured: set DATABASE_URL. See .env.example.")

    try:
        yield
    finally:
        await client.close()
        await database.close()
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="AI Second Brain API",
        description=(
            "Persistent graph-based memory engine. Phase 2: conversations, "
            "LLM-proposed memory extraction with a deterministic decision engine "
            "(no embeddings, no vector search, no authentication)."
        ),
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # CORS for a Vercel-hosted frontend. Explicit origins come from FRONTEND_URL and
    # CORS_ORIGINS; the regex additionally admits Vercel preview deployments, whose
    # hostnames are generated per commit and cannot be enumerated in advance.
    # Phase 9 (ADR 0022): credentialed CORS admits ONLY explicit origins (plus,
    # if opted in, one named Vercel project's previews -- never all of
    # vercel.app), and only the headers the API actually reads.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_origin_regex=settings.allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            REQUEST_ID_HEADER,
            *(["X-User-Id"] if settings.effective_auth_mode == "dev_header" else []),
        ],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach a request id to every response (ARCHITECTURE.md section 22)."""
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        """Return the application's own error taxonomy, never a bare 500."""
        payload = exc.to_payload()
        payload["error"]["request_id"] = getattr(request.state, "request_id", None)
        if exc.status_code >= 500:
            logger.error("%s: %s", exc.code, exc.message, extra={"details": exc.details})
        response = JSONResponse(
            status_code=exc.status_code, content=payload, headers=exc.headers or None
        )
        if exc.clear_session_cookie:
            clear_session_cookie(response, settings)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Report schema violations with the offending fields, not a generic message."""
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": "The request body or parameters are invalid",
                    "details": {"fields": _summarise_validation(exc)},
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Log the full traceback; return an opaque message plus the request id.

        Unexpected internals are not echoed to the client, but the request id makes
        the response traceable to the logged traceback.
        """
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled error (request_id=%s)", request_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected internal error occurred",
                    "request_id": request_id,
                }
            },
        )

    prefix = settings.api_prefix.rstrip("/")
    app.include_router(health.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(memories.router, prefix=prefix)
    app.include_router(entities.router, prefix=prefix)
    app.include_router(graph.router, prefix=prefix)
    app.include_router(conversations.router, prefix=prefix)
    app.include_router(documents.router, prefix=prefix)
    app.include_router(extraction.router, prefix=prefix)
    app.include_router(extraction.internal_router, prefix=prefix)
    app.include_router(embeddings.internal_router, prefix=prefix)
    app.include_router(search.router, prefix=prefix)
    app.include_router(review.router, prefix=prefix)
    app.include_router(review.internal_router, prefix=prefix)
    app.include_router(agent.router, prefix=prefix)

    return app


def _summarise_validation(exc: RequestValidationError) -> list[dict[str, str]]:
    summary: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        summary.append({"field": location or "body", "message": str(error.get("msg", ""))})
    return summary


app = create_app()
