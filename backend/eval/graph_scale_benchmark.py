"""Dense-graph, multi-hop retrieval scale benchmark (PHASE4_PLAN.md section 14).

Measures -- never assumes -- traversal latency, path/candidate counts, and
end-to-end hybrid-retrieval latency on deterministic synthetic graphs at
100 / 1,000 / 10,000 nodes, with a small number of artificially high-degree
"hub" entities to specifically stress the dense-graph case the plan calls out.

This exists to answer one question before writing any fan-out-capping code:
does `EntityRepository.neighbourhood_with_paths` (hops=2, `max_nodes` bound
from `settings.retrieval_graph_max_nodes`) actually misbehave against a dense
graph, or is the existing hop bound + node-count `LIMIT` already sufficient?
The user's explicit instruction: do NOT add fan-out capping unless this
benchmark demonstrates a concrete need, and never turn these numbers into a
production-scale claim -- this is one local machine, one Neo4j Community
instance, synthetic data.

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/graph_scale_benchmark.py
"""

from __future__ import annotations

import asyncio
import random
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.models.enums import MemoryStatus  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.services.hybrid_retrieval_service import HybridRetrievalService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402

NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: Node counts named in the plan. Small enough to run in one sitting on a
#: single local Neo4j Community instance -- these are relative comparison
#: points, not a production capacity claim.
SCALES = (100, 1_000, 10_000)

#: Fraction of nodes designated "hub" entities -- deliberately high in-degree,
#: to stress the traversal the way a real densely-connected concept (e.g. a
#: popular technology every project uses) would.
HUB_FRACTION = 0.01
MIN_HUBS = 2
#: Fraction of ALL other nodes each hub receives an incoming edge from.
HUB_IN_DEGREE_FRACTION = 0.2
#: Out-degree for an ordinary (non-hub) node in the random graph.
ORDINARY_OUT_DEGREE = 2

RETRIEVAL_HOPS = 2
RETRIEVAL_MAX_NODES = 200
TRIALS_PER_SCALE = 5


@dataclass
class ScaleResult:
    n_nodes: int
    n_edges: int
    seed_seconds: float
    hub_ids: list[str]
    traversal_ms_from_ordinary: list[float]
    traversal_ms_from_hub: list[float]
    hybrid_search_ms_from_hub: list[float]
    node_count_from_hub: int
    path_count_from_hub: int


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 -- deterministic synthetic data, not security


async def _seed_graph(
    graph: GraphClient, user_id: str, n_nodes: int
) -> tuple[list[str], list[str], int, float]:
    """Bulk-create `n_nodes` entities, one memory mentioning each, and a
    deterministic random-graph-plus-hubs edge set, via batched UNWIND writes
    (not the per-memory extraction pipeline -- this is synthetic setup data,
    not something the benchmark's timing includes).

    Returns (all_entity_ids, hub_entity_ids, edge_count, seed_seconds).
    """
    rng = _rng(n_nodes)
    # Salted with `user_id` (already run-unique) so re-running the benchmark
    # never collides with a prior run's leftover nodes -- entity ids are
    # otherwise deterministic *within* a run for reproducible sampling.
    entity_ids = [f"bench-{user_id}-{i}" for i in range(n_nodes)]
    n_hubs = max(MIN_HUBS, int(n_nodes * HUB_FRACTION))
    hub_ids = entity_ids[:n_hubs]
    ordinary_ids = entity_ids[n_hubs:]

    start = time.monotonic()

    entity_rows = [
        {
            "id": eid,
            "name": f"Entity {eid}",
            "normalized_name": f"entity {eid}",
            "entity_type": "concept",
        }
        for eid in entity_ids
    ]
    await graph.write(
        """
        UNWIND $entities AS e
        CREATE (n:Entity {
            id: e.id, user_id: $user_id, name: e.name, normalized_name: e.normalized_name,
            entity_type: e.entity_type, aliases: [], created_at: $now, updated_at: $now
        })
        """,
        {"entities": entity_rows, "user_id": user_id, "now": NOW},
    )

    memory_rows = [
        {"id": f"mem-{eid}", "content": f"A fact about {eid}", "entity_id": eid}
        for eid in entity_ids
    ]
    await graph.write(
        """
        UNWIND $memories AS m
        CREATE (mem:Memory {
            id: m.id, user_id: $user_id, content: m.content, memory_type: 'semantic',
            status: $status, confidence: 0.9, valid_from: null, valid_until: null,
            created_at: $now, updated_at: $now, invalidation_reason: null
        })
        WITH mem, m
        MATCH (e:Entity {id: m.entity_id, user_id: $user_id})
        CREATE (mem)-[:MENTIONS]->(e)
        """,
        {
            "memories": memory_rows,
            "user_id": user_id,
            "status": MemoryStatus.ACTIVE.value,
            "now": NOW,
        },
    )

    edges: list[dict[str, str]] = []
    for eid in ordinary_ids:
        for _ in range(ORDINARY_OUT_DEGREE):
            target = rng.choice(entity_ids)
            if target != eid:
                edges.append({"source_id": eid, "target_id": target})
    hub_in_degree = max(1, int(n_nodes * HUB_IN_DEGREE_FRACTION))
    for hub in hub_ids:
        sources = rng.sample(entity_ids, min(hub_in_degree, n_nodes))
        for source in sources:
            if source != hub:
                edges.append({"source_id": source, "target_id": hub})

    if edges:
        for i in range(0, len(edges), 5000):  # batch writes to keep transactions bounded
            batch = edges[i : i + 5000]
            await graph.write(
                """
                UNWIND $edges AS e
                MATCH (src:Entity {id: e.source_id, user_id: $user_id})
                MATCH (tgt:Entity {id: e.target_id, user_id: $user_id})
                CREATE (src)-[r:RELATES_TO {
                    id: randomUUID(), user_id: $user_id, predicate: 'relates_to',
                    status: $status, confidence: 0.9, memory_id: null,
                    valid_from: null, valid_until: null, created_at: $now, updated_at: $now
                }]->(tgt)
                """,
                {
                    "edges": batch,
                    "user_id": user_id,
                    "status": MemoryStatus.ACTIVE.value,
                    "now": NOW,
                },
            )

    seed_seconds = time.monotonic() - start
    return entity_ids, hub_ids, len(edges), seed_seconds


async def _time_traversal(
    entity_repo: EntityRepository, *, user_id: str, entity_id: str, entity_name: str
) -> tuple[float, int, int]:
    from app.models.domain import Entity

    root = Entity(
        id=entity_id,
        user_id=user_id,
        name=entity_name,
        normalized_name=entity_name.lower(),
        entity_type="concept",
        aliases=[],
        created_at=NOW,
        updated_at=NOW,
    )
    start = time.perf_counter()
    nodes = await entity_repo.neighbourhood_with_paths(
        user_id=user_id,
        root=root,
        hops=RETRIEVAL_HOPS,
        max_hops=3,
        excluded_statuses=[],
        max_nodes=RETRIEVAL_MAX_NODES,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    total_path_steps = sum(len(n.path) for n in nodes)
    return elapsed_ms, len(nodes), total_path_steps


async def run_scale(db: Database, graph: GraphClient, settings, n_nodes: int) -> ScaleResult:  # noqa: ANN001
    user_id = f"bench-user-{n_nodes}-{uuid.uuid4().hex[:6]}"
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=NOW)

    entity_ids, hub_ids, n_edges, seed_seconds = await _seed_graph(graph, user_id, n_nodes)
    entity_repo = EntityRepository(graph)

    rng = _rng(n_nodes + 1)
    ordinary_sample = [eid for eid in entity_ids if eid not in hub_ids]
    probe_ordinary = rng.sample(ordinary_sample, min(TRIALS_PER_SCALE, len(ordinary_sample)))
    probe_hub = hub_ids[: min(TRIALS_PER_SCALE, len(hub_ids))]

    ordinary_latencies = []
    for eid in probe_ordinary:
        ms, _, _ = await _time_traversal(
            entity_repo, user_id=user_id, entity_id=eid, entity_name=f"Entity {eid}"
        )
        ordinary_latencies.append(ms)

    hub_latencies = []
    node_count_from_hub = 0
    path_count_from_hub = 0
    for hub in probe_hub:
        ms, node_count, path_steps = await _time_traversal(
            entity_repo, user_id=user_id, entity_id=hub, entity_name=f"Entity {hub}"
        )
        hub_latencies.append(ms)
        node_count_from_hub = max(node_count_from_hub, node_count)
        path_count_from_hub = max(path_count_from_hub, path_steps)

    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: NOW)
    retrieval_service = RetrievalService(entity_repo, max_hops=3, now_fn=lambda: NOW)
    hybrid_latencies = []
    async with db.session() as session:
        hybrid = HybridRetrievalService(
            session=session,
            memory_service=memory_service,
            retrieval_service=retrieval_service,
            embedding_client=FakeEmbeddingClient(dimension=settings.embedding_dimension),
            settings=settings,
            now_fn=lambda: NOW,
        )
        for hub in probe_hub:
            query = f"What is entity {hub} related to?"
            start = time.perf_counter()
            await hybrid.search(user_id=user_id, query=query)
            hybrid_latencies.append((time.perf_counter() - start) * 1000)

    # Clean up this run's synthetic data so repeated benchmark runs don't
    # accumulate leftover nodes in whatever Neo4j instance this points at.
    await graph.write(
        "MATCH (n) WHERE n.user_id = $user_id DETACH DELETE n", {"user_id": user_id}
    )

    return ScaleResult(
        n_nodes=n_nodes,
        n_edges=n_edges,
        seed_seconds=seed_seconds,
        hub_ids=hub_ids,
        traversal_ms_from_ordinary=ordinary_latencies,
        traversal_ms_from_hub=hub_latencies,
        hybrid_search_ms_from_hub=hybrid_latencies,
        node_count_from_hub=node_count_from_hub,
        path_count_from_hub=path_count_from_hub,
    )


def _fmt(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"mean={statistics.mean(values):.2f}ms max={max(values):.2f}ms"


async def main() -> int:
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()

    print("Dense-graph multi-hop retrieval scale benchmark (PHASE4_PLAN.md section 14)")
    print(
        f"hops={RETRIEVAL_HOPS} max_nodes={RETRIEVAL_MAX_NODES} trials_per_scale={TRIALS_PER_SCALE}"
    )
    print(
        "NOT a production-scale claim: one local machine, one Neo4j Community "
        "instance, synthetic data.\n"
    )

    results = []
    for n_nodes in SCALES:
        result = await run_scale(db, graph, settings, n_nodes)
        results.append(result)
        print(
            f"=== N = {n_nodes:,} nodes ({result.n_edges:,} edges, hubs={len(result.hub_ids)}) ==="
        )
        print(f"  seed time:                  {result.seed_seconds:.2f}s")
        print(f"  traversal from ordinary node: {_fmt(result.traversal_ms_from_ordinary)}")
        print(f"  traversal from HUB node:      {_fmt(result.traversal_ms_from_hub)}")
        print(f"  hybrid search() from HUB:     {_fmt(result.hybrid_search_ms_from_hub)}")
        print(
            f"  nodes returned from hub (<=max_nodes={RETRIEVAL_MAX_NODES}): "
            f"{result.node_count_from_hub}"
        )
        print(f"  total path steps returned from hub: {result.path_count_from_hub}")
        print()

    await db.close()
    await graph.close()

    print("Summary (raw measurements, no production-scale claim):")
    for result in results:
        hub_mean = (
            statistics.mean(result.traversal_ms_from_hub) if result.traversal_ms_from_hub else 0.0
        )
        print(f"  N={result.n_nodes:>6,}  hub traversal mean={hub_mean:.2f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
