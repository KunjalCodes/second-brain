"""HTTP layer tests.

The service layer is wired to in-memory doubles, so these tests cover exactly what
the API itself is responsible for: routing, status codes, request validation, error
shape, identity handling and CORS. No database is involved.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_memory_service, get_retrieval_service
from app.main import app
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService
from tests.conftest import JAN, MAR, MAY, FrozenClock, SequentialIds
from tests.unit.fakes import FakeEntityRepository, FakeMemoryRepository

USER = "user-a"
OTHER_USER = "user-b"


@pytest.fixture
def store() -> FakeMemoryRepository:
    return FakeMemoryRepository()


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(JAN)


@pytest.fixture
def client(store: FakeMemoryRepository, api_clock: FrozenClock) -> Iterator[TestClient]:
    ids = SequentialIds()
    memory_service = MemoryService(store, now_fn=api_clock, id_fn=ids)  # type: ignore[arg-type]
    retrieval_service = RetrievalService(
        FakeEntityRepository(store),  # type: ignore[arg-type]
        max_hops=4,
        now_fn=api_clock,
    )
    app.dependency_overrides[get_memory_service] = lambda: memory_service
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def uses_body(target: str = "EasyOCR", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "content": f"Drawing Extractor uses {target}",
        "memory_type": "semantic",
        "entities": [
            {"name": "Drawing Extractor", "entity_type": "project"},
            {"name": target, "entity_type": "technology"},
        ],
        "relationships": [
            {"source": "Drawing Extractor", "predicate": "uses", "target": target}
        ],
        "source": {"source_type": "conversation", "reference": "conv-1"},
    }
    body.update(overrides)
    return body


def create(client: TestClient, target: str = "EasyOCR", user: str = USER, **kw: Any) -> dict:
    response = client.post(
        "/memories", json=uses_body(target, **kw), headers={"X-User-Id": user}
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestHealth:
    def test_liveness_does_not_require_a_database(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_liveness_includes_a_request_id_header(self, client: TestClient) -> None:
        assert client.get("/health").headers.get("X-Request-ID")

    def test_supplied_request_id_is_kept_for_correlation_but_never_canonical(
        self, client: TestClient
    ) -> None:
        # Phase 11 (M5): the canonical id is always server-generated.
        response = client.get("/health", headers={"X-Request-ID": "trace-123"})
        assert response.headers["X-Request-ID"] != "trace-123"
        assert response.headers["X-Client-Request-ID"] == "trace-123"

    def test_malformed_client_request_id_is_dropped(self, client: TestClient) -> None:
        response = client.get("/health", headers={"X-Request-ID": "x\r\nforged: 1" + "a" * 80})
        assert "X-Client-Request-ID" not in response.headers


class TestCreateMemory:
    def test_returns_201_with_the_stored_memory(self, client: TestClient) -> None:
        body = create(client)
        assert body["memory"]["content"] == "Drawing Extractor uses EasyOCR"
        assert body["memory"]["status"] == "active"
        assert len(body["entities"]) == 2
        assert body["relationships"][0]["predicate"] == "uses"
        assert body["sources"][0]["reference"] == "conv-1"

    def test_rejects_blank_content_with_field_detail(self, client: TestClient) -> None:
        response = client.post("/memories", json={"content": "   "})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "request_validation_error"
        assert error["details"]["fields"]

    def test_rejects_unknown_fields(self, client: TestClient) -> None:
        """extra="forbid" keeps a typo from being silently ignored."""
        response = client.post("/memories", json={"content": "x", "confidenc": 0.5})
        assert response.status_code == 422

    def test_rejects_out_of_range_confidence(self, client: TestClient) -> None:
        response = client.post("/memories", json={"content": "x", "confidence": 2})
        assert response.status_code == 422

    def test_rejects_undeclared_relationship_endpoint(self, client: TestClient) -> None:
        response = client.post(
            "/memories",
            json={
                "content": "x",
                "entities": [{"name": "A", "entity_type": "project"}],
                "relationships": [{"source": "A", "predicate": "uses", "target": "B"}],
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_rejects_creating_a_memory_as_superseded(self, client: TestClient) -> None:
        response = client.post("/memories", json={"content": "x", "status": "superseded"})
        assert response.status_code == 422


class TestGetMemory:
    def test_returns_the_memory(self, client: TestClient) -> None:
        created = create(client)
        response = client.get(
            f"/memories/{created['memory']['id']}", headers={"X-User-Id": USER}
        )
        assert response.status_code == 200
        assert response.json()["memory"]["id"] == created["memory"]["id"]

    def test_unknown_id_returns_404_with_a_code(self, client: TestClient) -> None:
        response = client.get("/memories/ghost", headers={"X-User-Id": USER})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestUpdateMemory:
    def test_patches_metadata(self, client: TestClient) -> None:
        created = create(client)
        response = client.patch(
            f"/memories/{created['memory']['id']}",
            json={"confidence": 0.4},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 200
        assert response.json()["confidence"] == 0.4

    def test_rejects_content_change(self, client: TestClient) -> None:
        created = create(client)
        response = client.patch(
            f"/memories/{created['memory']['id']}",
            json={"content": "rewritten"},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 422

    def test_rejects_setting_invalid_via_patch(self, client: TestClient) -> None:
        """The response must point at the endpoint that also closes the edges."""
        created = create(client)
        response = client.patch(
            f"/memories/{created['memory']['id']}",
            json={"status": "invalid"},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 422
        assert "invalidate" in response.text

    def test_illegal_transition_returns_409(self, client: TestClient) -> None:
        created = create(client)
        client.post(
            f"/memories/{created['memory']['id']}/invalidate",
            json={"reason": "wrong"},
            headers={"X-User-Id": USER},
        )
        response = client.patch(
            f"/memories/{created['memory']['id']}",
            json={"status": "active"},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"


class TestInvalidate:
    def test_invalidates_and_keeps_the_record(self, client: TestClient) -> None:
        created = create(client)
        memory_id = created["memory"]["id"]
        response = client.post(
            f"/memories/{memory_id}/invalidate",
            json={"reason": "I misremembered"},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "invalid"
        assert response.json()["invalidation_reason"] == "I misremembered"
        # Still readable: retraction is recorded, not erased.
        assert client.get(f"/memories/{memory_id}", headers={"X-User-Id": USER}).status_code == 200

    def test_there_is_no_delete_verb(self, client: TestClient) -> None:
        """Memory is append-only, so DELETE must not exist at all."""
        created = create(client)
        response = client.delete(
            f"/memories/{created['memory']['id']}", headers={"X-User-Id": USER}
        )
        assert response.status_code == 405


class TestSearch:
    def test_search_route_is_not_shadowed_by_the_id_route(self, client: TestClient) -> None:
        """GET /memories/search must not be parsed as memory id "search"."""
        response = client.get("/memories/search", headers={"X-User-Id": USER})
        assert response.status_code == 200
        assert "items" in response.json()

    def test_filters_by_query(self, client: TestClient) -> None:
        create(client, "EasyOCR")
        create(client, "Gemini")
        response = client.get(
            "/memories/search", params={"q": "gemini"}, headers={"X-User-Id": USER}
        )
        assert response.json()["total"] == 1

    def test_repeatable_status_filter(self, client: TestClient) -> None:
        create(client)
        response = client.get(
            "/memories/search",
            params=[("status", "active"), ("status", "historical")],
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_rejects_an_unknown_status_value(self, client: TestClient) -> None:
        response = client.get(
            "/memories/search", params={"status": "bogus"}, headers={"X-User-Id": USER}
        )
        assert response.status_code == 422

    def test_as_of_filter(self, client: TestClient, api_clock: FrozenClock) -> None:
        old = create(client, "EasyOCR")
        api_clock.advance_to(MAY)
        create(client, "PaddleOCR", supersedes=[old["memory"]["id"]])
        march = client.get(
            "/memories/search",
            params={"as_of": MAR.isoformat()},
            headers={"X-User-Id": USER},
        )
        contents = [m["content"] for m in march.json()["items"]]
        assert contents == ["Drawing Extractor uses EasyOCR"]

    def test_limit_is_capped_by_configuration(self, client: TestClient) -> None:
        response = client.get(
            "/memories/search", params={"limit": 100000}, headers={"X-User-Id": USER}
        )
        assert response.status_code == 200
        assert response.json()["limit"] <= 200

    def test_rejects_negative_offset(self, client: TestClient) -> None:
        response = client.get(
            "/memories/search", params={"offset": -1}, headers={"X-User-Id": USER}
        )
        assert response.status_code == 422


class TestEntities:
    def _project_id(self, client: TestClient, user: str = USER) -> str:
        response = client.get(
            "/entities/by-name",
            params={"name": "Drawing Extractor"},
            headers={"X-User-Id": user},
        )
        assert response.status_code == 200, response.text
        return response.json()["id"]

    def test_entity_detail(self, client: TestClient) -> None:
        create(client)
        response = client.get(
            f"/entities/{self._project_id(client)}", headers={"X-User-Id": USER}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["entity"]["name"] == "Drawing Extractor"
        assert body["memory_count"] == 1

    def test_unknown_entity_is_404(self, client: TestClient) -> None:
        response = client.get("/entities/ghost", headers={"X-User-Id": USER})
        assert response.status_code == 404

    def test_relationships(self, client: TestClient) -> None:
        create(client)
        response = client.get(
            f"/entities/{self._project_id(client)}/relationships",
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 200
        assert response.json()[0]["target_entity"]["name"] == "EasyOCR"

    def test_relationship_direction_validation(self, client: TestClient) -> None:
        create(client)
        response = client.get(
            f"/entities/{self._project_id(client)}/relationships",
            params={"direction": "sideways"},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 422

    def test_history_splits_current_and_historical(
        self, client: TestClient, api_clock: FrozenClock
    ) -> None:
        old = create(client, "EasyOCR")
        api_clock.advance_to(MAY)
        create(client, "PaddleOCR", supersedes=[old["memory"]["id"]])
        response = client.get(
            f"/entities/{self._project_id(client)}/history", headers={"X-User-Id": USER}
        )
        assert response.status_code == 200
        body = response.json()
        assert [v["target_entity"]["name"] for v in body["current"]] == ["PaddleOCR"]
        assert [v["target_entity"]["name"] for v in body["historical"]] == ["EasyOCR"]

    def test_history_as_of_a_past_date(
        self, client: TestClient, api_clock: FrozenClock
    ) -> None:
        old = create(client, "EasyOCR")
        api_clock.advance_to(MAY)
        create(client, "PaddleOCR", supersedes=[old["memory"]["id"]])
        response = client.get(
            f"/entities/{self._project_id(client)}/history",
            params={"as_of": MAR.isoformat()},
            headers={"X-User-Id": USER},
        )
        body = response.json()
        assert [v["target_entity"]["name"] for v in body["current"]] == ["EasyOCR"]

    def test_graph_neighbourhood(self, client: TestClient) -> None:
        create(client)
        response = client.get(
            f"/entities/{self._project_id(client)}/graph",
            params={"hops": 1},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["root"]["name"] == "Drawing Extractor"
        assert [n["entity"]["name"] for n in body["nodes"]] == ["EasyOCR"]

    def test_graph_rejects_hops_above_the_limit(self, client: TestClient) -> None:
        create(client)
        response = client.get(
            f"/entities/{self._project_id(client)}/graph",
            params={"hops": 99},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_entity_search_requires_a_query(self, client: TestClient) -> None:
        assert client.get("/entities/search", headers={"X-User-Id": USER}).status_code == 422


class TestUserIsolationOverHttp:
    def test_one_user_cannot_read_anothers_memory(self, client: TestClient) -> None:
        created = create(client, user=USER)
        response = client.get(
            f"/memories/{created['memory']['id']}", headers={"X-User-Id": OTHER_USER}
        )
        assert response.status_code == 404, "must not confirm the record exists"

    def test_one_user_cannot_patch_anothers_memory(self, client: TestClient) -> None:
        created = create(client, user=USER)
        response = client.patch(
            f"/memories/{created['memory']['id']}",
            json={"confidence": 0.1},
            headers={"X-User-Id": OTHER_USER},
        )
        assert response.status_code == 404

    def test_one_user_cannot_invalidate_anothers_memory(self, client: TestClient) -> None:
        created = create(client, user=USER)
        response = client.post(
            f"/memories/{created['memory']['id']}/invalidate",
            json={"reason": "x"},
            headers={"X-User-Id": OTHER_USER},
        )
        assert response.status_code == 404

    def test_search_is_scoped_per_user(self, client: TestClient) -> None:
        create(client, "EasyOCR", user=USER)
        response = client.get("/memories/search", headers={"X-User-Id": OTHER_USER})
        assert response.json()["total"] == 0

    def test_requests_fall_back_to_the_configured_dev_user(self, client: TestClient) -> None:
        """With no header, the configured DEV_USER_ID is used."""
        response = client.post("/memories", json=uses_body())
        assert response.status_code == 201
        assert response.json()["memory"]["user_id"] == "test-user"

    def test_blank_user_header_is_rejected(self, client: TestClient) -> None:
        response = client.get("/memories/search", headers={"X-User-Id": "   "})
        assert response.status_code == 422

    def test_absurdly_long_user_header_is_rejected(self, client: TestClient) -> None:
        response = client.get("/memories/search", headers={"X-User-Id": "x" * 500})
        assert response.status_code == 422


class TestCors:
    """Phase 9 (ADR 0022): credentialed CORS admits only explicit origins, plus
    -- if opted in -- ONE named Vercel project's previews. Before Phase 9 every
    `*.vercel.app` site was admitted (PHASE9_PLAN.md F7); these tests replace
    the ones that asserted that behaviour."""

    @staticmethod
    def _app(monkeypatch: pytest.MonkeyPatch, **env: str):  # noqa: ANN205
        from app.core.config import get_settings
        from app.main import create_app

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return TestClient(create_app())

    def test_arbitrary_vercel_origin_is_refused_by_default(self, client: TestClient) -> None:
        response = client.get("/health", headers={"Origin": "https://evil.vercel.app"})
        assert response.headers.get("access-control-allow-origin") is None

    def test_explicit_frontend_origin_is_allowed_with_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with self._app(monkeypatch, FRONTEND_URL="https://brain.example.com") as c:
            response = c.options(
                "/memories",
                headers={
                    "Origin": "https://brain.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://brain.example.com"
        assert response.headers["access-control-allow-credentials"] == "true"
        assert "POST" in response.headers.get("access-control-allow-methods", "")

    def test_only_the_named_vercel_project_previews_are_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with self._app(
            monkeypatch,
            CORS_ALLOW_VERCEL_PREVIEWS="true",
            CORS_VERCEL_PROJECT="brain",
            CORS_VERCEL_TEAM_SCOPE="me",
        ) as c:
            ok = c.get("/health", headers={"Origin": "https://brain-git-abc123-me.vercel.app"})
            other = c.get("/health", headers={"Origin": "https://evil.vercel.app"})
            lookalike = c.get("/health", headers={"Origin": "https://evil-brain.vercel.app"})
            # Phase 11 (S7): someone else's project named "brain-evil" used to
            # match `brain(-[a-z0-9-]+)?.vercel.app`.
            prefixed = c.get("/health", headers={"Origin": "https://brain-evil.vercel.app"})
            other_scope = c.get(
                "/health", headers={"Origin": "https://brain-git-abc123-attacker.vercel.app"}
            )
        assert prefixed.headers.get("access-control-allow-origin") is None
        assert other_scope.headers.get("access-control-allow-origin") is None
        assert ok.headers.get("access-control-allow-origin") == (
            "https://brain-git-abc123-me.vercel.app"
        )
        assert other.headers.get("access-control-allow-origin") is None
        assert lookalike.headers.get("access-control-allow-origin") is None

    def test_arbitrary_request_headers_are_not_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with self._app(monkeypatch, FRONTEND_URL="https://brain.example.com") as c:
            response = c.options(
                "/memories",
                headers={
                    "Origin": "https://brain.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "x-evil",
                },
            )
        assert response.status_code == 400

    def test_the_memory_space_header_is_allowed_cross_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with self._app(monkeypatch, FRONTEND_URL="https://brain.example.com") as c:
            response = c.options(
                "/memories",
                headers={
                    "Origin": "https://brain.example.com",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-memory-space",
                },
            )
        assert response.status_code == 200

    def test_unlisted_origin_is_not_granted_access(self, client: TestClient) -> None:
        response = client.get("/health", headers={"Origin": "https://evil.example.com"})
        assert response.headers.get("access-control-allow-origin") is None


class TestOpenApi:
    def test_schema_is_served(self, client: TestClient) -> None:
        assert client.get("/openapi.json").status_code == 200

    def test_all_phase_one_endpoints_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        expected = {
            "/health",
            "/health/ready",
            "/memories",
            "/memories/search",
            "/memories/{memory_id}",
            "/memories/{memory_id}/invalidate",
            "/entities/search",
            "/entities/by-name",
            "/entities/{entity_id}",
            "/entities/{entity_id}/relationships",
            "/entities/{entity_id}/memories",
            "/entities/{entity_id}/history",
            "/entities/{entity_id}/graph",
        }
        assert expected <= set(paths)
