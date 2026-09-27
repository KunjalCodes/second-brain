"""Phase 11 (M4): no error path returns internals to a client.

Every error class is forced through the real app (handlers, middleware) with
messages/details that DO contain internals -- SQL, Cypher, file paths,
credentials, hostnames -- and the response bodies are scanned for them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_client, get_db
from app.core.errors import (
    ConfigurationError,
    DatabaseError,
    DatabaseUnavailableError,
    GraphQueryError,
    GraphUnavailableError,
    LLMRateLimitedError,
    LLMUnavailableError,
    StorageUnavailableError,
)
from app.main import create_app

SECRETS = (
    "Traceback",
    "SELECT ",
    "MATCH (",
    "/Users/",
    "/var/lib",
    "bolt://",
    "postgresql://",
    "sk-live-",
    "GEMINI_API_KEY",
    "INTERNAL_API_TOKEN",
    "db-host.internal",
)

POISON = (
    "SELECT * FROM users; MATCH (m) RETURN m; /Users/x/secret; bolt://db-host.internal; "
    "postgresql://u:p@db-host.internal/db; sk-live-123; GEMINI_API_KEY; INTERNAL_API_TOKEN"
)

ERRORS = {
    "graph_unavailable": GraphUnavailableError("The graph database is currently unreachable."),
    "graph_query": GraphQueryError("The graph query failed", details={"cypher": POISON}),
    "db_unavailable": DatabaseUnavailableError("down", details={"reason": POISON}),
    "db_error": DatabaseError("The database query failed", details={"sql": POISON}),
    "configuration": ConfigurationError(f"Check GEMINI_API_KEY ({POISON})"),
    "storage": StorageUnavailableError("storage down", details={"reason": POISON}),
    "llm": LLMUnavailableError("Gemini is unavailable (HTTP 503)", details={"raw": POISON}),
    "llm_429": LLMRateLimitedError("Gemini is rate limiting requests", retry_after_seconds=12),
    "unexpected": RuntimeError(POISON),
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()

    async def boom(kind: str) -> None:
        raise ERRORS[kind]

    app.add_api_route("/__boom/{kind}", boom, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _assert_clean(body: str) -> None:
    leaked = [s for s in SECRETS if s in body]
    assert leaked == [], f"response leaked {leaked}: {body[:300]}"


@pytest.mark.parametrize("kind", sorted(ERRORS))
def test_error_bodies_never_contain_internals(client: TestClient, kind: str) -> None:
    r = client.get(f"/__boom/{kind}")
    assert r.status_code >= 500
    _assert_clean(r.text)
    body = r.json()["error"]
    assert set(body) <= {"code", "message", "request_id"}, "5xx bodies carry no details"
    assert body["request_id"]


def test_rate_limited_provider_error_is_503_with_retry_after(client: TestClient) -> None:
    r = client.get("/__boom/llm_429")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "12"
    assert r.json()["error"]["code"] == "llm_rate_limited"


def test_framework_errors_use_the_envelope(client: TestClient) -> None:
    missing = client.get("/no/such/route")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    wrong_method = client.delete("/health")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"


class _DeadGraph:
    is_connected = False

    async def connect(self) -> None:
        raise GraphUnavailableError(f"cannot connect to {POISON}")

    async def verify(self) -> None:
        raise GraphUnavailableError(POISON)


class _DeadDb:
    is_connected = False

    async def connect(self) -> None:
        raise DatabaseUnavailableError(f"OperationalError {POISON}")

    async def verify(self) -> None:
        raise DatabaseUnavailableError(POISON)


def test_readiness_reports_codes_only(client: TestClient) -> None:
    client.app.dependency_overrides[get_client] = lambda: _DeadGraph()
    client.app.dependency_overrides[get_db] = lambda: _DeadDb()
    r = client.get("/health/ready")
    assert r.status_code == 503
    _assert_clean(r.text)
    assert r.json() == {
        "status": "unavailable",
        "checks": {
            "neo4j": "unavailable",
            "postgresql": "unavailable",
            "migrations": "unavailable",
        },
    }


def test_liveness_never_touches_dependencies(client: TestClient) -> None:
    client.app.dependency_overrides[get_client] = lambda: _DeadGraph()
    client.app.dependency_overrides[get_db] = lambda: _DeadDb()
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
