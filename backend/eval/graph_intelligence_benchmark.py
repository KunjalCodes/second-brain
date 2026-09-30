# ruff: noqa: E501 - result tables kept one per line
"""Phase 15 (M14) performance of the graph-intelligence endpoints at scale.

    cd backend && uv run python eval/graph_intelligence_benchmark.py [--sizes 100,500,1000,5000,10000]

Reuses Phase 4's deterministic synthetic graph (`graph_scale_benchmark._seed_graph`:
N entities, one memory each, ~2 random out-edges per ordinary entity, 1% hubs
with in-degree 0.2*N) and times the REAL routes in-process (ASGI; no network):
median / max of 5 calls, payload bytes, and the bound each response reports.
Local, one Neo4j Community instance, synthetic data: characterisation only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# This harness drives the app in-process with the development identity header
# (X-User-Id), so it must NOT inherit a session-mode environment. Forced, not
# defaulted: an inherited AUTH_MODE=session made every call a silent 401.
# (The app itself refuses this identity mode in production-like environments.)
os.environ["AUTH_MODE"] = "dev_header"
os.environ["ALLOW_USER_ID_HEADER"] = "true"

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.api.deps import get_client, get_db, get_embeddings  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.main import app  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from eval.graph_scale_benchmark import NOW, _seed_graph  # noqa: E402

RUNS = 5


async def _time(c: AsyncClient, uid: str, method: str, url: str, **kw) -> dict:
    lat, size, body, status = [], 0, None, None
    for _ in range(RUNS):
        t0 = time.perf_counter()
        r = await c.request(method, url, headers={"X-User-Id": uid}, **kw)
        lat.append((time.perf_counter() - t0) * 1000)
        size, status = len(r.content), r.status_code
        body = r.json()
    return {"status": status, "median_ms": round(statistics.median(lat), 1), "max_ms": round(max(lat), 1),
            "bytes": size, "body": body}  # fmt: skip


async def _delete_graph(graph: GraphClient, uid: str) -> None:
    """Delete one synthetic user's graph in bounded batches. A single
    `DETACH DELETE` of a 10,000-entity / 200k-edge graph is one huge
    transaction; under load it failed and left the data behind."""
    while True:
        rows = await graph.write(
            "MATCH (n {user_id: $u}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c",
            {"u": uid},
        )
        if not rows or rows[0]["c"] == 0:
            return


async def bench_size(db: Database, graph: GraphClient, n: int) -> dict:
    uid = f"bench-gi-{n}-{uuid.uuid4().hex[:6]}"
    async with db.session() as s:
        await UserRepository(s).ensure_exists(uid, now=NOW)
    entity_ids, hub_ids, n_edges, seed_s = await _seed_graph(graph, uid, n)
    rng = random.Random(n)
    ordinary = entity_ids[len(hub_ids):]
    a, b = rng.choice(ordinary), rng.choice(ordinary)
    hub = hub_ids[0]
    edge = await graph.read(
        "MATCH (x:Entity {id: $a, user_id: $u})-[r:RELATES_TO]->(y:Entity {user_id: $u}) RETURN y.id AS y LIMIT 1",
        {"a": a, "u": uid},
    )
    out: dict = {"entities": n, "edges": n_edges, "hubs": len(hub_ids), "seed_s": round(seed_s, 1)}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://b") as c:
            cases = {
                "paths_ordinary": ("GET", "/graph/paths", {"params": {"source_entity_id": a, "target_entity_id": b}}),
                "paths_from_hub": ("GET", "/graph/paths", {"params": {"source_entity_id": hub, "target_entity_id": a}}),
                "context_hub": ("GET", f"/entities/{hub}/context", {}),
                "context_ordinary": ("GET", f"/entities/{a}/context", {}),
                "neighborhood_hub_2hop_historical": ("GET", f"/graph/entities/{hub}/neighborhood", {"params": {"hops": 2, "temporal_scope": "historical"}}),
                "neighborhood_hub_2hop_current": ("GET", f"/graph/entities/{hub}/neighborhood", {"params": {"hops": 2}}),
                "hybrid_search_same_query": ("POST", "/search", {"json": {"query": f"How is Entity {a} related to Entity {hub}?", "limit": 8}}),
                "context_subgraph": ("POST", "/graph/context", {"json": {"query": f"How is Entity {a} related to Entity {hub}?"}}),
            }
            if edge:
                cases["explain_edge"] = ("GET", "/graph/edges/explain", {"params": {"source_entity_id": a, "target_entity_id": edge[0]["y"], "predicate": "relates_to"}})
            for name, (method, url, kw) in cases.items():
                res = await _time(c, uid, method, url, **kw)
                body = res.pop("body")
                bound = {}
                if name.startswith("paths"):
                    bound = {"explored_nodes": body.get("explored_nodes"), "paths": len(body.get("paths", [])),
                             "reason": body.get("reason"), "truncated": body.get("truncated")}  # fmt: skip
                elif name.startswith("context_") and name != "context_subgraph":
                    bound = {"current": len(body.get("current_relationships", [])),
                             "historical": len(body.get("historical_relationships", [])),
                             "truncated": body.get("truncated_relationships")}  # fmt: skip
                elif name.startswith("neighborhood"):
                    bound = {"nodes": len(body.get("nodes", [])), "edges": len(body.get("edges", [])),
                             "truncated_nodes": body.get("truncated_nodes")}  # fmt: skip
                elif name == "context_subgraph":
                    bound = {"nodes": len(body.get("nodes", [])), "edges": len(body.get("edges", [])),
                             "anchors": len(body.get("anchors", [])), "truncated": body.get("truncated")}  # fmt: skip
                out[name] = {**res, **bound}
    finally:
        await _delete_graph(graph, uid)
        async with db.session() as s:
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    return out


async def main_async(sizes: list[int]) -> list[dict]:
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient()
    try:
        results = []
        for n in sizes:
            r = await bench_size(db, graph, n)
            results.append(r)
            print(f"=== {n} entities, {r['edges']} edges (seed {r['seed_s']}s)")
            for k, v in r.items():
                if isinstance(v, dict):
                    print(f"  {k:<34} {v}")
        return results
    finally:
        app.dependency_overrides.clear()
        await db.close()
        await graph.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="100,500,1000,5000,10000")
    p.add_argument("--json", type=Path)
    args = p.parse_args()
    results = asyncio.run(main_async([int(x) for x in args.sizes.split(",")]))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
