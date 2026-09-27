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
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.body_limit import MULTIPART_OVERHEAD_BYTES, BodySizeLimitMiddleware
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
    ops,
    review,
    search,
)
from app.api.routes.health import APP_VERSION
from app.core.config import get_settings
from app.core.errors import AppError, ConfigurationError
from app.core.logging import bind_request, configure_logging, reset_request
from app.core.metrics import metrics
from app.db.engine import get_database
from app.embeddings.factory import get_embedding_client
from app.graph.connection import get_graph_client
from app.llm.factory import get_llm_client
from app.storage.factory import get_document_storage
from app.workers.sweeper import Sweeper, set_sweeper

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
CLIENT_REQUEST_ID_HEADER = "X-Client-Request-ID"
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
    "magnetometer=(), gyroscope=(), accelerometer=(), browsing-topics=()"
)
_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
access_logger = logging.getLogger("app.access")

#: Phase 10 (G2): every API response that is not in the public set below is
#: explicitly uncacheable -- user memory data must never land in a browser,
#: proxy or CDN cache. One central rule, so a new route cannot forget it.
NO_STORE = "no-store"
#: Paths (without the API prefix) that carry no user data and may be cached.
PUBLIC_CACHEABLE_PATHS = frozenset(
    {"/health", "/health/ready", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the graph driver on startup, close it on shutdown.

    A failure to reach the graph is logged at ERROR and surfaced by
    ``/health/ready``, but does not prevent startup: on a managed host, refusing to
    boot turns a transient database problem into a restart loop that is much harder
    to diagnose than an unhealthy-but-running service.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.effective_log_format)
    logger.info("Starting %s %s (env=%s)", settings.app_name, APP_VERSION, settings.app_env)

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

    sweeper: Sweeper | None = None
    if settings.sweep_interval_seconds > 0 and settings.database_configured:
        sweeper = Sweeper(
            db=database,
            graph_client=client,
            llm_client_fn=get_llm_client,
            embedding_client_fn=get_embedding_client,
            storage_fn=get_document_storage,
            settings=settings,
        )
        sweeper.start()
        set_sweeper(sweeper)

    try:
        yield
    finally:
        if sweeper is not None:
            await sweeper.stop()
            set_sweeper(None)
        await client.close()
        await database.close()
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.effective_log_format)

    app = FastAPI(
        title="AI Second Brain API",
        description=(
            "Persistent graph-based memory engine: temporal memory in Neo4j, "
            "conversation and document extraction with a deterministic decision "
            "engine, hybrid retrieval, a memory agent, and session authentication."
        ),
        version=APP_VERSION,
        lifespan=lifespan,
        # Phase 11 (D8): interactive docs are off in production-like
        # environments unless EXPOSE_DOCS=true. `app.openapi()` still works
        # (the frontend's type generation calls it directly, not over HTTP).
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # G2: the docs are served at the root; health also under the API prefix.
    prefix_for_cache = settings.api_prefix.rstrip("/")
    public_cacheable_paths = frozenset(
        {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
        | {prefix_for_cache + p for p in PUBLIC_CACHEABLE_PATHS}
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
        expose_headers=[REQUEST_ID_HEADER, CLIENT_REQUEST_ID_HEADER, "Retry-After"],
    )

    security_headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": PERMISSIONS_POLICY,
    }
    if settings.is_production_like:
        security_headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Correlation, access logging, metrics and G2 caching, for every request.

        Phase 11 (M5): the canonical request id is ALWAYS server-generated. A
        client-supplied `X-Request-ID` is never trusted as the canonical id; a
        well-formed one (`[A-Za-z0-9._-]{1,64}`) is kept only as
        `client_request_id` for correlation and echoed as
        `X-Client-Request-ID`; anything else is dropped (log-injection guard).

        One access line per request records the route TEMPLATE, never the raw
        path or query string -- search text never reaches the logs.
        """
        request_id = str(uuid.uuid4())
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        client_request_id = supplied if _CLIENT_REQUEST_ID.fullmatch(supplied) else None
        request.state.request_id = request_id
        token = bind_request(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            metrics.observe_request(
                method=request.method, route=template, status=status_code, ms=elapsed_ms
            )
            access_logger.info(
                "%s %s %d %.1fms",
                request.method,
                template,
                status_code,
                elapsed_ms,
                extra={
                    "event": "http_request",
                    "method": request.method,
                    "route": template,
                    "status": status_code,
                    "latency_ms": elapsed_ms,
                    "client_request_id": client_request_id,
                },
            )
            reset_request(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        # Phase 11 (M10): server processing time, so a client can separate
        # backend time from network time (PerformanceResourceTiming).
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms}"
        if client_request_id:
            response.headers[CLIENT_REQUEST_ID_HEADER] = client_request_id
        if request.url.path not in public_cacheable_paths:
            response.headers["Cache-Control"] = NO_STORE
        # Phase 11 (S9): baseline headers on every API response. HSTS only
        # where HTTPS is guaranteed (production-like environments refuse
        # non-https origins); browsers ignore it over plain HTTP anyway.
        for header, value in security_headers.items():
            response.headers.setdefault(header, value)
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        """Return the application's own error taxonomy, never a bare 500.

        Phase 11 (M4): server-side failures (5xx) never return `details` --
        they can carry driver/OS text such as file paths -- and a configuration
        error never returns its message, which names settings and providers.
        Both stay in the log, correlated by request id.
        """
        payload = exc.to_payload()
        request_id = getattr(request.state, "request_id", None)
        payload["error"]["request_id"] = request_id
        metrics.observe_error(code=exc.code, status=exc.status_code, details=exc.details)
        if exc.status_code >= 500:
            logger.error(
                "app_error code=%s status=%d request_id=%s message=%s details=%s",
                exc.code,
                exc.status_code,
                request_id,
                exc.message,
                exc.details,
            )
            payload["error"].pop("details", None)
            if isinstance(exc, ConfigurationError):
                payload["error"]["message"] = (
                    "The service is not configured correctly; the operator has been "
                    "notified through the server logs"
                )
        response = JSONResponse(
            status_code=exc.status_code, content=payload, headers=exc.headers or None
        )
        if exc.clear_session_cookie:
            clear_session_cookie(response, settings)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Framework-raised errors (unknown route 404, 405, the disabled
        registration route) in the same envelope as everything else (Phase 11)."""
        codes = {404: "not_found", 405: "method_not_allowed"}
        messages = {404: "Not Found", 405: "Method Not Allowed"}
        return JSONResponse(
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
            content={
                "error": {
                    "code": codes.get(exc.status_code, f"http_{exc.status_code}"),
                    "message": messages.get(exc.status_code, "Request failed"),
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

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
        metrics.observe_error(code="internal_error", status=500)
        logger.exception("Unhandled error (request_id=%s)", request_id)
        # This handler runs outside the HTTP middleware, so set G2 here too.
        return JSONResponse(
            headers={"Cache-Control": NO_STORE},
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected internal error occurred",
                    "request_id": request_id,
                }
            },
        )

    # Outermost (added last): bound every request body before anything --
    # auth, multipart parsing, JSON decoding -- reads it (Phase 11, S10).
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=settings.max_request_body_bytes,
        document_upload_limit=settings.max_document_size_bytes + MULTIPART_OVERHEAD_BYTES,
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
    app.include_router(ops.internal_router, prefix=prefix)

    return app


def _summarise_validation(exc: RequestValidationError) -> list[dict[str, str]]:
    summary: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        summary.append({"field": location or "body", "message": str(error.get("msg", ""))})
    return summary


app = create_app()
