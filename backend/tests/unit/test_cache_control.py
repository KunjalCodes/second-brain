"""Phase 10 (G2): authenticated API responses are explicitly uncacheable.

User memory data must never be stored by a browser, proxy or CDN cache. The
rule is applied centrally (one middleware + the unhandled-error handler), so
these tests check it across success, application-error, validation-error,
auth and server-error responses, and check that the public, data-free paths
are NOT forced uncacheable.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_memory_service, get_retrieval_service
from app.main import app
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService
from tests.conftest import JAN, FrozenClock, SequentialIds
from tests.unit.fakes import FakeEntityRepository, FakeMemoryRepository

USER = {"X-User-Id": "cache-user"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    store = FakeMemoryRepository()
    clock = FrozenClock(JAN)
    app.dependency_overrides[get_memory_service] = lambda: MemoryService(
        store,  # type: ignore[arg-type]
        now_fn=clock,
        id_fn=SequentialIds(),
    )
    app.dependency_overrides[get_retrieval_service] = lambda: RetrievalService(
        FakeEntityRepository(store),  # type: ignore[arg-type]
        max_hops=4,
        now_fn=clock,
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("GET", "/memories/search", 200),  # authenticated success
        ("GET", "/memories/does-not-exist", 404),  # application error
        ("GET", "/memories/search?limit=0", 422),  # validation error
        ("GET", "/auth/me", 200),  # identity payload
        ("GET", "/no-such-route", 404),  # framework 404
    ],
)
def test_user_facing_responses_are_no_store(
    client: TestClient, method: str, path: str, expected_status: int
) -> None:
    r = client.request(method, path, headers=USER)
    assert r.status_code == expected_status
    assert r.headers["cache-control"] == "no-store"


def test_created_memory_response_is_no_store(client: TestClient) -> None:
    r = client.post(
        "/memories",
        json={"content": "Atlas uses Gemini", "entities": [{"name": "Atlas"}]},
        headers=USER,
    )
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"


def test_unhandled_server_error_is_no_store(client: TestClient) -> None:
    def explode() -> None:
        raise RuntimeError("boom")

    app.dependency_overrides[get_memory_service] = explode
    r = client.get("/memories/search", headers=USER)
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/health", "/openapi.json", "/docs"])
def test_public_data_free_paths_are_not_forced_uncacheable(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers.get("cache-control") != "no-store"
