"""Stage 0 benchmark of the EXISTING interactive graph endpoint (Phase 8,
docs/PHASE8_PLAN.md section 15/16/18, decision D1).

`GET /entities/{id}/graph` is served by `RetrievalService.get_neighbourhood`
-> `EntityRepository.neighbourhood` (`NEIGHBOURHOOD_NODES_TEMPLATE`, no
`LIMIT`, hop bound up to `GRAPH_MAX_HOPS`, default 4). The Phase 4
benchmark (`eval/graph_scale_benchmark.py`) measured only the separately
capped retrieval path (`neighbourhood_with_paths`) and is NOT a substitute
for this one.

This measures -- never assumes -- the exact service call the route makes,
on the same deterministic synthetic hub graphs Phase 4 used (its `_seed_graph`
is reused, not copied), across low/medium/high-degree/hub roots and hops
1..GRAPH_MAX_HOPS, plus a merged-entity root, a cross-user attempt, and a
current-vs-historical edge distribution. Every call is bounded by a
wall-clock timeout; on timeout the server-side transaction is terminated so
a runaway traversal cannot keep consuming the local Neo4j instance.

One local machine, one Neo4j Community instance, synthetic data -- relative
comparison points, never a production capacity claim.

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/graph_endpoint_benchmark.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
import tracemalloc
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import NotFoundError  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from eval.graph_scale_benchmark import _seed_graph  # noqa: E402

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PAST = datetime(2025, 6, 1, tzinfo=UTC)
SCALES = (100, 1_000, 10_000)
TRIALS = 3
CALL_TIMEOUT_SECONDS = 60.0
#: Fraction of edges re-labelled `superseded` (window closed in the past), so
#: the current-vs-historical mix the endpoint returns can be observed.
SUPERSEDED_FRACTION = 0.3


@dataclass
class Measurement:
    scale: int
    root_kind: str
    root_degree: int
    hops: int
    latencies_ms: list[float] = field(default_factory=list)
    node_count: int | None = None
    edge_count: int | None = None
    superseded_edges: int | None = None
    payload_bytes: int | None = None
    peak_python_kib: float | None = None
    truncated: bool | None = None
    outcome: str = "ok"  # ok | timeout | error:<type>


async def _terminate_user_transactions(graph: GraphClient, user_id: str) -> None:
    """Best-effort: kill any still-running server transaction for this
    benchmark user after a client-side timeout, so it stops consuming Neo4j.
    """
    try:
        rows = await graph.read(
            "SHOW TRANSACTIONS YIELD transactionId, parameters "
            "WHERE parameters.user_id = $user_id RETURN transactionId",
            {"user_id": user_id},
        )
        for row in rows:
            await graph.write(f"TERMINATE TRANSACTION '{row['transactionId']}'")
    except Exception as exc:  # noqa: BLE001 - best effort; report, don't crash the run
        print(f"    (could not terminate server transaction: {type(exc).__name__}: {exc})")


async def _degrees(graph: GraphClient, user_id: str) -> list[tuple[str, int]]:
    rows = await graph.read(
        """
        MATCH (e:Entity {user_id: $user_id})
        RETURN e.id AS id, COUNT { (e)-[:RELATES_TO {user_id: $user_id}]-() } AS degree
        ORDER BY degree, id
        """,
        {"user_id": user_id},
    )
    return [(r["id"], int(r["degree"])) for r in rows]


def _pick_roots(degrees: list[tuple[str, int]], hub_ids: list[str]) -> dict[str, tuple[str, int]]:
    ordinary = [d for d in degrees if d[0] not in set(hub_ids)]
    by_id = dict(degrees)
    hub = max(hub_ids, key=lambda h: (by_id[h], h))

    def at(q: float) -> tuple[str, int]:
        return ordinary[min(len(ordinary) - 1, int(q * (len(ordinary) - 1)))]

    return {
        "low": at(0.5),
        "medium": at(0.95),
        "high": ordinary[-1],
        "hub": (hub, by_id[hub]),
    }


async def _supersede_fraction(graph: GraphClient, user_id: str, fraction: float) -> int:
    rows = await graph.read(
        "MATCH (:Entity {user_id: $user_id})-[r:RELATES_TO {user_id: $user_id}]->() "
        "RETURN count(r) AS n",
        {"user_id": user_id},
    )
    total = int(rows[0]["n"])
    k = int(total * fraction)
    await graph.write(
        """
        MATCH (:Entity {user_id: $user_id})-[r:RELATES_TO {user_id: $user_id}]->()
        WITH r ORDER BY r.id LIMIT $k
        SET r.status = 'superseded', r.valid_from = $past, r.valid_until = $now
        """,
        {"user_id": user_id, "k": k, "past": PAST, "now": NOW},
    )
    return k


async def _measure(
    service: RetrievalService, graph: GraphClient, *, user_id: str, entity_id: str,
    m: Measurement,
) -> None:
    for trial in range(TRIALS):
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                service.get_neighbourhood(user_id=user_id, entity_id=entity_id, hops=m.hops),
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            m.outcome = "timeout"
            await _terminate_user_transactions(graph, user_id)
            return
        except Exception as exc:  # noqa: BLE001 - record, never hide
            m.outcome = f"error:{type(exc).__name__}"
            return
        m.latencies_ms.append((time.perf_counter() - start) * 1000)
        if trial == 0:
            m.node_count = len(result.nodes)
            m.edge_count = len(result.relationships)
            m.superseded_edges = sum(
                1 for r in result.relationships if r.status.value == "superseded"
            )
            m.payload_bytes = len(result.model_dump_json().encode())
            m.truncated = getattr(result, "truncated", None)

    # One separate, untimed pass for peak Python allocation (tracemalloc slows
    # execution, so it is never mixed into the latency trials above).
    tracemalloc.start()
    try:
        await asyncio.wait_for(
            service.get_neighbourhood(user_id=user_id, entity_id=entity_id, hops=m.hops),
            timeout=CALL_TIMEOUT_SECONDS,
        )
        _, peak = tracemalloc.get_traced_memory()
        m.peak_python_kib = peak / 1024
    except TimeoutError:
        await _terminate_user_transactions(graph, user_id)
    finally:
        tracemalloc.stop()


def _fmt_ms(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"median={statistics.median(values):8.1f}ms max={max(values):8.1f}ms"


def _cap_kwargs(settings) -> dict[str, int]:  # noqa: ANN001
    """The D1 caps if this checkout has them (Stage 0 ran before they existed)."""
    if hasattr(settings, "graph_max_nodes"):
        return {"max_nodes": settings.graph_max_nodes, "max_edges": settings.graph_max_edges}
    return {}


async def run_scale(db: Database, graph: GraphClient, settings, n_nodes: int) -> list[Measurement]:  # noqa: ANN001
    user_id = f"bench-graph-{n_nodes}-{uuid.uuid4().hex[:6]}"
    other_user_id = f"bench-graph-other-{uuid.uuid4().hex[:6]}"
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=NOW)

    entity_ids, hub_ids, n_edges, seed_s = await _seed_graph(graph, user_id, n_nodes)
    superseded = await _supersede_fraction(graph, user_id, SUPERSEDED_FRACTION)
    degrees = await _degrees(graph, user_id)
    roots = _pick_roots(degrees, hub_ids)
    print(
        f"=== N={n_nodes:,} entities, {n_edges:,} edges ({superseded:,} superseded), "
        f"hubs={len(hub_ids)}, seed {seed_s:.1f}s ==="
    )

    # Built exactly as `app.api.deps.get_retrieval_service` builds it, so the
    # measured path is the route's own (including the D1 caps, once present).
    service = RetrievalService(
        EntityRepository(graph),
        max_hops=settings.graph_max_hops,
        now_fn=lambda: NOW,
        **_cap_kwargs(settings),
    )
    results: list[Measurement] = []

    for kind, (root_id, degree) in roots.items():
        for hops in range(1, settings.graph_max_hops + 1):
            m = Measurement(scale=n_nodes, root_kind=kind, root_degree=degree, hops=hops)
            await _measure(service, graph, user_id=user_id, entity_id=root_id, m=m)
            results.append(m)
            print(
                f"  {kind:6} deg={degree:5} hops={hops}  {_fmt_ms(m.latencies_ms)}  "
                f"nodes={m.node_count} edges={m.edge_count} "
                f"superseded_in_result={m.superseded_edges} "
                f"payload={m.payload_bytes} B "
                f"peak_py={m.peak_python_kib and round(m.peak_python_kib)} KiB "
                f"truncated={m.truncated} [{m.outcome}]"
            )
            if m.outcome != "ok":
                break  # deeper hops from this root can only be worse

    # Merged entity: a synthetic merged-away node redirected to the hub.
    merged_id = f"bench-merged-{uuid.uuid4().hex[:6]}"
    await graph.write(
        """
        MATCH (hub:Entity {id: $hub_id, user_id: $user_id})
        CREATE (m:Entity {id: $merged_id, user_id: $user_id, name: 'Merged Away',
                          normalized_name: 'merged away ' + $merged_id,
                          entity_type: 'concept', aliases: [],
                          created_at: $now, updated_at: $now})
        CREATE (m)-[:MERGED_INTO {user_id: $user_id, created_at: $now, merge_id: 'bench'}]->(hub)
        """,
        {"hub_id": roots["hub"][0], "user_id": user_id, "merged_id": merged_id, "now": NOW},
    )
    start = time.perf_counter()
    merged_result = await service.get_neighbourhood(
        user_id=user_id, entity_id=merged_id, hops=1
    )
    merged_ms = (time.perf_counter() - start) * 1000
    print(
        f"  merged-away root -> resolved root={merged_result.root.id == roots['hub'][0]} "
        f"(redirect info in response: none) {merged_ms:.1f}ms "
        f"nodes={len(merged_result.nodes)}"
    )

    # Cross-user: another user asks for this user's hub.
    start = time.perf_counter()
    try:
        await service.get_neighbourhood(user_id=other_user_id, entity_id=roots["hub"][0], hops=2)
        cross = "LEAK -- data returned"
    except NotFoundError:
        cross = "NotFoundError (404)"
    print(f"  cross-user attempt: {cross} {(time.perf_counter() - start) * 1000:.1f}ms")
    print()

    await graph.write("MATCH (n) WHERE n.user_id = $user_id DETACH DELETE n", {"user_id": user_id})
    async with db.session() as session:
        from sqlalchemy import text

        await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
    return results


async def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--scales", default=",".join(str(n) for n in SCALES))
    parser.add_argument("--label", default="before")
    args = parser.parse_args()
    scales = [int(x) for x in args.scales.split(",")]
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    print(
        f"[{args.label}] existing GET /entities/{{id}}/graph "
        "(RetrievalService.get_neighbourhood)"
    )
    print(
        f"GRAPH_MAX_HOPS={settings.graph_max_hops} caps={_cap_kwargs(settings) or 'none'} "
        f"trials={TRIALS} "
        f"timeout={CALL_TIMEOUT_SECONDS:.0f}s -- synthetic data, one local Community instance\n"
    )
    try:
        for n in scales:
            await run_scale(db, graph, settings, n)
    finally:
        await db.close()
        await graph.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
