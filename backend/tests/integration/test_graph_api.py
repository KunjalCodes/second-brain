"""HTTP-level tests for /graph/* and the D2 `/entities/search` change,
through the real app against real Neo4j + PostgreSQL (docs/PHASE8_PLAN.md
sections 9 and 14): routing, validation, clamping, identity via the existing
`X-User-Id` seam, and cross-user 404s.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_client, get_db
from app.main import app
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService
from app.services.retrieval_service import DEFAULT_EXCLUDED, RetrievalService
from tests.conftest import JAN

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(db, graph):  # noqa: ANN001
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _rel(content: str, subject: str, predicate: str, obj: str) -> MemoryCreate:
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [
                {"name": subject, "entity_type": "project"},
                {"name": obj, "entity_type": "technology"},
            ],
            "relationships": [{"source": subject, "predicate": predicate, "target": obj}],
        }
    )


@pytest.fixture
async def seeded(db, graph, user_id, cleanup, pg_cleanup) -> dict[str, Any]:  # noqa: ANN001
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
    memories = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
    gem = await memories.create(
        user_id=user_id, payload=_rel("Atlas uses Gemini", "Atlas", "uses", "Gemini")
    )
    await memories.create(
        user_id=user_id, payload=_rel("Atlas Legacy uses Python", "Atlas Legacy", "uses", "Python")
    )
    retrieval = RetrievalService(EntityRepository(graph), max_hops=4)
    ids = {
        n: (await retrieval.find_entity_by_name(user_id=user_id, name=n)).id
        for n in ("Atlas", "Gemini", "Atlas Legacy")
    }
    await EntityRepository(graph).merge(
        user_id=user_id,
        entity_id_a=ids["Atlas"],
        entity_id_b=ids["Atlas Legacy"],
        merge_id=str(uuid.uuid4()),
        new_aliases=["Atlas Legacy"],
        excluded_statuses=DEFAULT_EXCLUDED,
        now=JAN,
        id_fn=lambda: str(uuid.uuid4()),
    )
    return {"ids": ids, "memory": gem.memory.id}


def _h(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


class TestEndpoints:
    async def test_neighborhood(self, client, user_id, seeded) -> None:  # noqa: ANN001
        r = await client.get(
            f"/graph/entities/{seeded['ids']['Atlas']}/neighborhood", headers=_h(user_id)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["temporal_scope"] == "current"
        assert body["truncated_nodes"] is False
        assert {n["label"] for n in body["nodes"]} == {"Atlas", "Gemini", "Python"}
        assert body["budget"] == {
            "max_nodes": 150,
            "max_edges": 400,
            "max_hops": 1,
            "max_memories_per_entity": None,
        }

    async def test_neighborhood_by_merged_id_reports_redirect(
        self,
        client,
        user_id,
        seeded,  # noqa: ANN001
    ) -> None:
        r = await client.get(
            f"/graph/entities/{seeded['ids']['Atlas Legacy']}/neighborhood", headers=_h(user_id)
        )
        body = r.json()
        assert body["root"] == seeded["ids"]["Atlas"]
        assert body["resolved_from"] == seeded["ids"]["Atlas Legacy"]
        assert body["redirect_depth"] == 1

    async def test_overview(self, client, user_id, seeded) -> None:  # noqa: ANN001
        r = await client.get(
            "/graph/overview", params={"include": ["memories", "merges"]}, headers=_h(user_id)
        )
        assert r.status_code == 200
        labels = {n["label"] for n in r.json()["nodes"]}
        assert "Atlas Legacy" not in labels and "Atlas" in labels

    async def test_evidence(self, client, user_id, seeded) -> None:  # noqa: ANN001
        r = await client.get(
            "/graph/edges/evidence",
            params={
                "source_entity_id": seeded["ids"]["Atlas"],
                "target_entity_id": seeded["ids"]["Gemini"],
                "predicate": "uses",
            },
            headers=_h(user_id),
        )
        assert r.status_code == 200
        body = r.json()
        assert [a["memory_id"] for a in body["assertions"]] == [seeded["memory"]]
        assert body["assertions"][0]["evidence_status"] == "no_sources"
        assert body["evidence_available"] is False

    async def test_timeline(self, client, user_id, seeded) -> None:  # noqa: ANN001
        r = await client.get(f"/graph/memories/{seeded['memory']}/timeline", headers=_h(user_id))
        assert r.status_code == 200
        assert [n["id"] for n in r.json()["nodes"]] == [seeded["memory"]]

    async def test_existing_graph_endpoint_shape_is_preserved_plus_additive_flag(
        self,
        client,
        user_id,
        seeded,  # noqa: ANN001
    ) -> None:
        """D1 adds exactly one field (`truncated`); every pre-Phase-8 key stays."""
        r = await client.get(f"/entities/{seeded['ids']['Atlas']}/graph", headers=_h(user_id))
        assert r.status_code == 200
        assert set(r.json()) == {"root", "hops", "nodes", "relationships", "truncated"}
        assert r.json()["truncated"] is False


class TestValidationAndBounds:
    @pytest.mark.parametrize(
        "params",
        [
            {"hops": 3},  # above GRAPH_EXPLORE_MAX_HOPS=2
            {"hops": 0},
            {"temporal_scope": "as_of"},  # as_of missing
            {"as_of": "2026-03-01T00:00:00Z"},  # instant with scope=current
            {"temporal_scope": "as_of", "as_of": "2026-03-01T00:00:00"},  # naive
            {"temporal_scope": "yesterday"},
            {"predicate": "Uses; DROP"},
            {"predicate": [f"p{i}x" for i in range(21)]},
            {"entity_type": "spaceship"},
            {"include": "everything"},
            {"exclude_ids": "not an id!"},
            {"max_nodes": 0},
        ],
    )
    async def test_rejected_with_422(self, client, user_id, seeded, params) -> None:  # noqa: ANN001
        r = await client.get(
            f"/graph/entities/{seeded['ids']['Atlas']}/neighborhood",
            params=params,
            headers=_h(user_id),
        )
        assert r.status_code == 422, r.text

    async def test_too_many_exclude_ids_is_rejected(
        self,
        client,
        user_id,
        seeded,  # noqa: ANN001
    ) -> None:
        r = await client.get(
            f"/graph/entities/{seeded['ids']['Atlas']}/neighborhood",
            params={"exclude_ids": [f"id-{i}" for i in range(151)]},
            headers=_h(user_id),
        )
        assert r.status_code == 422

    async def test_malformed_path_id_is_rejected(self, client, user_id) -> None:  # noqa: ANN001
        r = await client.get("/graph/memories/bad%20id%27/timeline", headers=_h(user_id))
        assert r.status_code == 422

    async def test_budgets_are_clamped_not_raised(
        self,
        client,
        user_id,
        seeded,  # noqa: ANN001
    ) -> None:
        r = await client.get(
            "/graph/overview",
            params={"limit": 100_000, "max_edges": 100_000},
            headers=_h(user_id),
        )
        assert r.json()["budget"]["max_nodes"] == 150
        assert r.json()["budget"]["max_edges"] == 400

    async def test_no_mutating_methods_exist(self, client, user_id, seeded) -> None:  # noqa: ANN001
        for method in ("post", "put", "patch", "delete"):
            r = await getattr(client, method)("/graph/overview", headers=_h(user_id))
            assert r.status_code == 405


class TestIsolationOverHttp:
    async def test_other_user_gets_404_or_nothing(
        self,
        client,
        user_id,
        other_user_id,
        seeded,  # noqa: ANN001
    ) -> None:
        ids = seeded["ids"]
        other = _h(other_user_id)
        for path in (
            f"/graph/entities/{ids['Atlas']}/neighborhood",
            f"/graph/entities/{ids['Atlas Legacy']}/neighborhood",
            f"/graph/memories/{seeded['memory']}/timeline",
        ):
            assert (await client.get(path, headers=other)).status_code == 404
        r = await client.get(
            "/graph/edges/evidence",
            params={
                "source_entity_id": ids["Atlas"],
                "target_entity_id": ids["Gemini"],
                "predicate": "uses",
            },
            headers=other,
        )
        assert r.status_code == 404
        overview = (await client.get("/graph/overview", headers=other)).json()
        assert overview["nodes"] == [] and overview["edges"] == []


class TestEntitySearchD2:
    async def test_merged_away_hidden_by_default_and_available_on_request(
        self,
        client,
        user_id,
        other_user_id,
        seeded,  # noqa: ANN001
    ) -> None:
        r = await client.get("/entities/search", params={"q": "atlas"}, headers=_h(user_id))
        assert [e["name"] for e in r.json()["items"]] == ["Atlas"]
        r = await client.get(
            "/entities/search", params={"q": "atlas", "include_merged": True}, headers=_h(user_id)
        )
        assert [e["name"] for e in r.json()["items"]] == ["Atlas", "Atlas Legacy"]
        # The merged id still resolves (to the canonical entity).
        r = await client.get(f"/entities/{seeded['ids']['Atlas Legacy']}", headers=_h(user_id))
        assert r.json()["entity"]["id"] == seeded["ids"]["Atlas"]
        r = await client.get(
            "/entities/search",
            params={"q": "atlas", "include_merged": True},
            headers=_h(other_user_id),
        )
        assert r.json()["items"] == []
