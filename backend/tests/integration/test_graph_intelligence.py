# ruff: noqa: E501 - request tables kept one per line
"""Phase 15 graph-intelligence endpoints, against real Neo4j and PostgreSQL.

Contract, isolation and bounds tests. Behavioural coverage over a richer
synthetic world lives in eval/graph_intelligence_runner.py (28 tasks).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_client, get_db, get_embeddings
from app.embeddings.fake import FakeEmbeddingClient
from app.main import app
from app.repositories.memory_repository import MemoryRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.graph_intelligence_service import RelationKind, relation_kind
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.integration

Y2024 = datetime(2024, 1, 1, tzinfo=UTC)
Y2025 = datetime(2025, 3, 1, tzinfo=UTC)


def _m(content: str, rels: list[list[str]], types: dict[str, str], **kw) -> MemoryCreate:  # noqa: ANN003
    names = sorted({n for a, _, b in rels for n in (a, b)})
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [{"name": n, "entity_type": types.get(n, "other")} for n in names],
            "relationships": [{"source": a, "predicate": p, "target": b} for a, p, b in rels],
            "source": {"source_type": "user_statement", "reference": "test"},
            **kw,
        }
    )


TYPES = {"Kunjal": "person", "Acme": "company", "Globex": "company", "Atlas": "project",
         "FastAPI": "technology", "Pydantic": "technology"}  # fmt: skip


@pytest.fixture
async def world(db, graph, user_id, cleanup, pg_cleanup):  # noqa: ANN001, ANN201
    async with db.session() as s:
        await UserRepository(s).ensure_exists(user_id, now=Y2024)
    early = MemoryService(MemoryRepository(graph), now_fn=lambda: Y2024)
    later = MemoryService(MemoryRepository(graph), now_fn=lambda: Y2025)
    acme = await early.create(
        user_id=user_id,
        payload=_m("Kunjal works at Acme.", [["Kunjal", "works_at", "Acme"]], TYPES),
    )
    globex = await later.create(user_id=user_id, payload=_m(
        "Kunjal moved to Globex.", [["Kunjal", "works_at", "Globex"]], TYPES,
        supersedes=[acme.memory.id]))  # fmt: skip
    await later.create(
        user_id=user_id,
        payload=_m("Kunjal works on Atlas.", [["Kunjal", "works_on", "Atlas"]], TYPES),
    )
    await later.create(
        user_id=user_id, payload=_m("Atlas uses FastAPI.", [["Atlas", "uses", "FastAPI"]], TYPES)
    )
    await later.create(
        user_id=user_id,
        payload=_m("FastAPI depends on Pydantic.", [["FastAPI", "depends_on", "Pydantic"]], TYPES),
    )
    ids = {e.name: e.id for d in (acme, globex) for e in d.entities}
    rows = await graph.read(
        "MATCH (e:Entity {user_id: $u}) RETURN e.name AS n, e.id AS i", {"u": user_id}
    )
    ids.update({r["n"]: r["i"] for r in rows})
    return {"entities": ids, "acme": acme.memory.id, "globex": globex.memory.id}


@pytest.fixture
async def client(db, graph):  # noqa: ANN001, ANN201
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


def h(uid: str) -> dict[str, str]:
    return {"X-User-Id": uid}


async def test_paths_three_hops_labelled_inferred(client, world, user_id) -> None:  # noqa: ANN001
    e = world["entities"]
    r = await client.get(
        "/graph/paths",
        params={"source_entity_id": e["Kunjal"], "target_entity_id": e["Pydantic"]},
        headers=h(user_id),
    )
    assert r.status_code == 200, r.text
    top = r.json()["paths"][0]
    assert top["length"] == 3
    assert top["basis"] == "inferred_from_graph"
    assert [s["to_name"] for s in top["steps"]] == ["Atlas", "FastAPI", "Pydantic"]
    assert all(rel["memory_ids"] for s in top["steps"] for rel in s["relationships"])
    assert set(top["signals"]) == {"length", "min_support", "current_fraction", "min_confidence"}


async def test_paths_respect_the_hop_cap(client, world, user_id) -> None:  # noqa: ANN001
    e = world["entities"]
    r = await client.get(
        "/graph/paths",
        params={"source_entity_id": e["Kunjal"], "target_entity_id": e["Pydantic"],
                "max_hops": 99, "max_paths": 999},
        headers=h(user_id),
    )  # fmt: skip
    body = r.json()
    assert body["max_hops"] <= 4 and body["max_paths"] <= 20  # clamped, never raised


async def test_historical_scope_returns_only_what_is_no_longer_true(client, world, user_id) -> None:  # noqa: ANN001
    kunjal = world["entities"]["Kunjal"]
    r = await client.get(
        f"/graph/entities/{kunjal}/neighborhood",
        params={"temporal_scope": "historical"},
        headers=h(user_id),
    )
    assert r.status_code == 200, r.text
    labels = {n["id"]: n["label"] for n in r.json()["nodes"]}
    edges = {
        (x["predicate"], labels[x["target"]])
        for x in r.json()["edges"]
        if x["kind"] == "relates_to"
    }
    assert edges == {("works_at", "Acme")}


async def test_edge_explanation_says_why_it_is_no_longer_true(client, world, user_id) -> None:  # noqa: ANN001
    e = world["entities"]
    r = await client.get(
        "/graph/edges/explain",
        params={"source_entity_id": e["Kunjal"], "target_entity_id": e["Acme"],
                "predicate": "works_at", "temporal_scope": "all"},
        headers=h(user_id),
    )  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["temporal_state"] == "historical"
    assert "temporally_derived" in body["basis"]
    assert body["why"].startswith("No longer true")
    assert body["supporting_memories"][0]["superseded_by"] == [world["globex"]]
    missing = await client.get(
        "/graph/edges/explain",
        params={"source_entity_id": e["Kunjal"], "target_entity_id": e["Acme"],
                "predicate": "works_at"},
        headers=h(user_id),
    )  # fmt: skip
    assert missing.status_code == 404  # not current -> not in the current scope


async def test_entity_context_splits_current_and_historical(client, world, user_id) -> None:  # noqa: ANN001
    r = await client.get(f"/entities/{world['entities']['Kunjal']}/context", headers=h(user_id))
    assert r.status_code == 200, r.text
    body = r.json()
    current = {(x["predicate"], x["other_name"]) for x in body["current_relationships"]}
    historical = {(x["predicate"], x["other_name"]) for x in body["historical_relationships"]}
    assert ("works_at", "Globex") in current and ("works_at", "Acme") not in current
    assert ("works_at", "Acme") in historical
    assert body["memory_total"] == 3
    assert body["related_entities"][0]["signals"]


async def test_memory_evolution_orders_new_fact_then_supersession(client, world, user_id) -> None:  # noqa: ANN001
    r = await client.get(f"/memories/{world['acme']}/evolution", headers=h(user_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["kind"] for e in body["events"]] == ["new_fact", "supersession"]
    assert body["current_memory_ids"] == [world["globex"]]
    assert body["events"][1]["related_memory_ids"] == [world["acme"]]


async def test_context_subgraph_is_small_and_explained(client, world, user_id) -> None:  # noqa: ANN001
    r = await client.post(
        "/graph/context", json={"query": "What does FastAPI depend on?"}, headers=h(user_id)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    names = {n["label"] for n in body["nodes"] if n["kind"] == "entity"}
    assert {"FastAPI", "Pydantic"} <= names
    assert "Acme" not in names and "Globex" not in names
    assert all("score" in sig for sig in body["edge_signals"].values())
    assert body["construction"]["kept_entities"] <= body["max_nodes"]


async def test_context_query_length_and_budget_are_validated(client, world, user_id) -> None:  # noqa: ANN001
    too_long = await client.post("/graph/context", json={"query": "x" * 501}, headers=h(user_id))
    assert too_long.status_code == 422
    r = await client.post(
        "/graph/context", json={"query": "FastAPI", "max_nodes": 10_000}, headers=h(user_id)
    )
    assert r.json()["max_nodes"] <= 60


async def test_another_user_gets_404_everywhere(client, world, user_id, other_user_id) -> None:  # noqa: ANN001
    e = world["entities"]
    calls = [
        ("GET", "/graph/paths", {"params": {"source_entity_id": e["Kunjal"], "target_entity_id": e["Atlas"]}}),
        ("GET", "/graph/edges/explain", {"params": {"source_entity_id": e["Kunjal"], "target_entity_id": e["Atlas"], "predicate": "works_on"}}),
        ("GET", f"/entities/{e['Kunjal']}/context", {}),
        ("GET", f"/memories/{world['acme']}/evolution", {}),
    ]  # fmt: skip
    for method, url, kw in calls:
        r = await client.request(method, url, headers=h(other_user_id), **kw)
        assert r.status_code == 404, (url, r.status_code)
    ctx = await client.post(
        "/graph/context", json={"query": "Kunjal Atlas FastAPI"}, headers=h(other_user_id)
    )
    assert ctx.status_code == 200
    assert ctx.json()["nodes"] == [] and ctx.json()["evidence"] == []


def test_relation_kinds_come_from_the_stated_predicate_only() -> None:
    assert relation_kind("led_to") is RelationKind.CAUSAL
    assert relation_kind("fixed_by") is RelationKind.CAUSAL
    assert relation_kind("possible_cause") is RelationKind.POSSIBLE_CAUSE
    assert relation_kind("uses") is RelationKind.ASSOCIATION
    assert relation_kind("followed_by") is RelationKind.ASSOCIATION  # sequence is not cause


# -- path search edge cases (bidirectional BFS) ---------------------------------------------


def _service(db, graph, settings):  # noqa: ANN001, ANN202
    from app.repositories.entity_repository import EntityRepository
    from app.repositories.graph_repository import GraphRepository
    from app.services.graph_intelligence_service import GraphIntelligenceService
    from app.services.provenance_service import ProvenanceService
    from app.services.retrieval_service import RetrievalService

    ms = MemoryService(MemoryRepository(graph), now_fn=lambda: Y2025)
    rs = RetrievalService(EntityRepository(graph), max_hops=4)

    class _Session:  # paths / explain never touch PostgreSQL
        pass

    return GraphIntelligenceService(
        repository=GraphRepository(graph), retrieval=rs, memory=ms,
        provenance=ProvenanceService(_Session(), ms), session=_Session(), settings=settings,
    )  # fmt: skip


async def _link(graph, user_id: str, edges: list[tuple[str, str, str]]) -> dict[str, str]:  # noqa: ANN001
    ms = MemoryService(MemoryRepository(graph), now_fn=lambda: Y2025)
    for a, p, b in edges:
        await ms.create(user_id=user_id, payload=_m(f"{a} {p} {b}.", [[a, p, b]], {}))
    rows = await graph.read(
        "MATCH (e:Entity {user_id: $u}) RETURN e.name AS n, e.id AS i", {"u": user_id}
    )
    return {r["n"]: r["i"] for r in rows}


@pytest.fixture
async def plain_user(db, user_id, cleanup, pg_cleanup):  # noqa: ANN001, ANN201
    async with db.session() as s:
        await UserRepository(s).ensure_exists(user_id, now=Y2024)
    return user_id


async def test_all_shortest_paths_are_returned_deterministically(db, graph, plain_user) -> None:  # noqa: ANN001
    from app.core.config import get_settings

    ids = await _link(
        graph,
        plain_user,
        [
            ("S", "uses", "M1"),
            ("M1", "uses", "T"),
            ("S", "uses", "M2"),
            ("M2", "uses", "T"),
            ("S", "uses", "L1"),
            ("L1", "uses", "L2"),
            ("L2", "uses", "T"),
        ],
    )
    svc = _service(db, graph, get_settings())
    runs = []
    for _ in range(3):
        r = await svc.paths(
            user_id=plain_user, source_entity_id=ids["S"], target_entity_id=ids["T"]
        )
        runs.append([p.entity_ids for p in r.paths])
    assert runs[0] == runs[1] == runs[2]
    assert {tuple(x) for x in runs[0]} == {
        (ids["S"], ids["M1"], ids["T"]),
        (ids["S"], ids["M2"], ids["T"]),
    }
    assert all(len(x) == 3 for x in runs[0])  # the longer S-L1-L2-T route is not a shortest path


async def test_a_cycle_does_not_loop_or_repeat_entities(db, graph, plain_user) -> None:  # noqa: ANN001
    from app.core.config import get_settings

    ids = await _link(
        graph,
        plain_user,
        [("A", "uses", "B"), ("B", "uses", "C"), ("C", "uses", "A"), ("C", "uses", "D")],
    )
    svc = _service(db, graph, get_settings())
    r = await svc.paths(user_id=plain_user, source_entity_id=ids["A"], target_entity_id=ids["D"])
    assert [p.entity_ids for p in r.paths] == [[ids["A"], ids["C"], ids["D"]]]
    for p in r.paths:
        assert len(set(p.entity_ids)) == len(p.entity_ids)


async def test_the_node_budget_is_honoured_and_reported(db, graph, plain_user) -> None:  # noqa: ANN001
    """Both ends are hubs, so neither side can be searched cheaply, and the
    connecting entities sort AFTER the leaves and fall outside the budget."""
    from app.core.config import get_settings

    edges = (
        [("HubA", "uses", f"A{i}") for i in range(40)]
        + [("HubB", "uses", f"B{i}") for i in range(40)]
        + [("HubA", "uses", "zz-x"), ("zz-x", "uses", "zz-y"), ("zz-y", "uses", "HubB")]
    )
    ids = await _link(graph, plain_user, edges)
    small = get_settings().model_copy(update={"graph_explore_max_nodes": 8})
    r = await _service(db, graph, small).paths(
        user_id=plain_user, source_entity_id=ids["HubA"], target_entity_id=ids["HubB"]
    )
    assert r.paths == []
    assert r.reason == "node_budget_exhausted" and r.truncated is True
    assert r.explored_nodes <= 8  # the budget bounds work even when it finds nothing


async def test_a_hub_is_connected_to_a_distant_entity_inside_a_tiny_budget(
    db, graph, plain_user
) -> None:  # noqa: ANN001
    """Bidirectional search expands the cheap side: a 40-leaf hub three steps
    from a low-degree entity is found without touching the hub's leaves."""
    from app.core.config import get_settings

    edges = [("Hub", "uses", f"Leaf{i}") for i in range(40)] + [
        ("Hub", "uses", "Mid1"),
        ("Mid1", "uses", "Mid2"),
        ("Mid2", "uses", "T"),
    ]
    ids = await _link(graph, plain_user, edges)
    small = get_settings().model_copy(update={"graph_explore_max_nodes": 8})
    r = await _service(db, graph, small).paths(
        user_id=plain_user, source_entity_id=ids["Hub"], target_entity_id=ids["T"]
    )
    assert [p.entity_ids for p in r.paths] == [[ids["Hub"], ids["Mid1"], ids["Mid2"], ids["T"]]]
    assert r.explored_nodes <= 8 and r.truncated is False


async def test_a_disconnected_pair_says_so_and_is_not_an_error(db, graph, plain_user) -> None:  # noqa: ANN001
    from app.core.config import get_settings

    ids = await _link(graph, plain_user, [("P", "uses", "Q"), ("R", "uses", "S")])
    r = await _service(db, graph, get_settings()).paths(
        user_id=plain_user, source_entity_id=ids["P"], target_entity_id=ids["S"]
    )
    assert r.paths == [] and r.reason == "not_connected_within_hops" and r.truncated is False


async def test_the_same_entity_is_not_a_path(db, graph, plain_user) -> None:  # noqa: ANN001
    from app.core.config import get_settings

    ids = await _link(graph, plain_user, [("P", "uses", "Q")])
    r = await _service(db, graph, get_settings()).paths(
        user_id=plain_user, source_entity_id=ids["P"], target_entity_id=ids["P"]
    )
    assert r.reason == "same_entity" and r.paths == []


async def test_a_predicate_filter_restricts_the_paths(db, graph, plain_user) -> None:  # noqa: ANN001
    from app.core.config import get_settings

    ids = await _link(graph, plain_user, [("A", "uses", "B"), ("B", "depends_on", "C")])
    svc = _service(db, graph, get_settings())
    both = await svc.paths(user_id=plain_user, source_entity_id=ids["A"], target_entity_id=ids["C"])
    only_uses = await svc.paths(
        user_id=plain_user,
        source_entity_id=ids["A"],
        target_entity_id=ids["C"],
        predicates=["uses"],
    )
    assert len(both.paths) == 1 and only_uses.paths == []


async def test_a_path_can_run_against_the_stored_direction(db, graph, plain_user) -> None:  # noqa: ANN001
    from app.core.config import get_settings

    ids = await _link(graph, plain_user, [("A", "depends_on", "B")])
    r = await _service(db, graph, get_settings()).paths(
        user_id=plain_user, source_entity_id=ids["B"], target_entity_id=ids["A"]
    )
    step = r.paths[0].steps[0]
    assert step.relationships[0].direction == "backward"  # stored A -> B, asked B ... A
    assert step.relationships[0].predicate == "depends_on"


async def test_a_merged_away_endpoint_resolves_to_the_canonical_entity(
    db, graph, plain_user
) -> None:  # noqa: ANN001
    import uuid

    from app.core.config import get_settings
    from app.repositories.entity_repository import EntityRepository

    ids = await _link(graph, plain_user, [("X", "uses", "OpenAI"), ("X", "uses", "Open AI")])
    await EntityRepository(graph).merge(
        user_id=plain_user, entity_id_a=ids["OpenAI"], entity_id_b=ids["Open AI"], merge_id="m",
        new_aliases=["Open AI"], excluded_statuses=[], now=Y2025, id_fn=lambda: str(uuid.uuid4()),
    )  # fmt: skip
    r = await _service(db, graph, get_settings()).paths(
        user_id=plain_user, source_entity_id=ids["Open AI"], target_entity_id=ids["X"]
    )
    assert r.source_entity_id == ids["OpenAI"]
    assert r.source_resolved_from == ids["Open AI"]
    assert r.paths and r.paths[0].entity_ids[0] == ids["OpenAI"]
