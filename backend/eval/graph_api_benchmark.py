"""Performance benchmark for the Phase 8 memory graph exploration API
(docs/PHASE8_PLAN.md section 16, Stage 7).

Measures -- never assumes -- the four `/graph/*` service calls on the same
deterministic synthetic hub graphs as Stage 0 (`eval/graph_scale_benchmark.py`'s
`_seed_graph`, reused), at 100 / 1k / 10k entities:

* neighborhood: low / medium / high-degree / hub roots, hops 1-2, current and
  all scopes, and `include=memories`;
* overview: degree-ranked landing slice, current and all;
* evidence: scale-independence on a hub edge, plus a dedicated fixture for
  no evidence / one memory / many memories / many sources, with Neo4j-read
  and SQL-statement counts (the N+1 check);
* timeline: chain lengths 1 / 5 / 20 / 25 (bounded at 20);
* the three canonical explorer questions answered with the pre-Phase-8
  endpoints vs the new ones, counted as real HTTP requests through the app.

Every slice is checked against its own budget (a hard assertion, reported as
a violation count, never averaged away). `RELATES_TO.memory_id` is
back-filled on the seeded graph so the per-edge asserting-memory check in
every scope predicate is actually exercised.

One local machine, one Neo4j Community instance, synthetic data -- relative
comparison points, never a production capacity claim.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from app.api.deps import get_app_settings, get_client, get_db  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.conversation import MessageRole  # noqa: E402
from app.models.enums import SourceType  # noqa: E402
from app.repositories.conversation_repository import ConversationRepository  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.graph_repository import GraphRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.message_repository import MessageRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.schemas.graph import GraphInclude, GraphSlice, TemporalScope  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.services.graph_exploration_service import GraphExplorationService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.provenance_service import ProvenanceService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from eval.graph_endpoint_benchmark import (  # noqa: E402
    SUPERSEDED_FRACTION,
    _degrees,
    _pick_roots,
    _supersede_fraction,
)
from eval.graph_scale_benchmark import _seed_graph  # noqa: E402

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SCALES = (100, 1_000, 10_000)
TRIALS = 5

_PG_CLEANUP = (
    ("messages", "user_id"),
    ("conversations", "user_id"),
    ("users", "id"),
)

budget_violations = 0


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _fmt(values: list[float]) -> str:
    return (
        f"p50={statistics.median(values):8.1f}ms p95={_pct(values, 0.95):8.1f}ms "
        f"max={max(values):8.1f}ms"
    )


def _check_budget(result: Any) -> str:
    global budget_violations
    if isinstance(result, GraphSlice):
        over = len(result.nodes) > result.budget.max_nodes or (
            result.budget.max_edges is not None and len(result.edges) > result.budget.max_edges
        )
        if over:
            budget_violations += 1
            return "BUDGET-VIOLATION"
    return "within-budget"


async def _time(label: str, fn: Any) -> Any:
    latencies = []
    result = None
    for _ in range(TRIALS):
        start = time.perf_counter()
        result = await fn()
        latencies.append((time.perf_counter() - start) * 1000)
    payload = len(result.model_dump_json().encode())
    extra = ""
    if isinstance(result, GraphSlice):
        extra = (
            f"nodes={len(result.nodes):4} edges={len(result.edges):4} "
            f"trunc_n={result.truncated_nodes!s:5} trunc_e={result.truncated_edges!s:5} "
            f"{_check_budget(result)}"
        )
    print(f"  {label:<44} {_fmt(latencies)}  payload={payload:8} B  {extra}")
    return result


def _service(
    graph: GraphClient, settings: Settings, session: Any = None
) -> GraphExplorationService:
    memory = MemoryService(MemoryRepository(graph), now_fn=lambda: NOW)
    return GraphExplorationService(
        repository=GraphRepository(graph),
        retrieval=RetrievalService(EntityRepository(graph), max_hops=4, now_fn=lambda: NOW),
        memory=memory,
        provenance=ProvenanceService(session, memory) if session is not None else None,
        settings=settings,
        now_fn=lambda: NOW,
    )


async def _backfill_memory_ids(graph: GraphClient, user_id: str) -> None:
    """Give every seeded edge the id of its source entity's memory, in
    batches (GraphClient runs managed transactions, so no IN TRANSACTIONS)."""
    while True:
        rows = await graph.write(
            """
            MATCH (m:Memory {user_id: $u})-[:MENTIONS]->(a:Entity {user_id: $u})
                  -[r:RELATES_TO {user_id: $u}]->()
            WHERE r.memory_id IS NULL
            WITH m, r LIMIT 20000
            SET r.memory_id = m.id
            RETURN count(r) AS n
            """,
            {"u": user_id},
        )
        if not rows or rows[0]["n"] == 0:
            return


async def _cleanup(db: Database, graph: GraphClient, user_ids: list[str]) -> None:
    for uid in user_ids:
        while True:
            rows = await graph.write(
                "MATCH (n {user_id: $u}) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS n",
                {"u": uid},
            )
            if not rows or rows[0]["n"] == 0:
                break
    async with db.session() as session:
        for uid in user_ids:
            for table, col in _PG_CLEANUP:
                await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": uid})


async def bench_scale(db: Database, graph: GraphClient, settings: Settings, n: int) -> None:
    user_id = f"bench-graphapi-{n}-{uuid.uuid4().hex[:6]}"
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=NOW)
    try:
        _, hub_ids, n_edges, seed_s = await _seed_graph(graph, user_id, n)
        await _supersede_fraction(graph, user_id, SUPERSEDED_FRACTION)
        await _backfill_memory_ids(graph, user_id)
        roots = _pick_roots(await _degrees(graph, user_id), hub_ids)
        print(
            f"=== N={n:,} entities, {n_edges:,} edges, hubs={len(hub_ids)}, seed {seed_s:.1f}s ==="
        )
        service = _service(graph, settings)
        for kind, (root, degree) in roots.items():
            for hops in (1, 2):
                for scope in (TemporalScope.CURRENT, TemporalScope.ALL):
                    await _time(
                        f"neighborhood {kind:6} deg={degree:<5} h={hops} {scope.value}",
                        lambda r=root, h=hops, s=scope: service.neighborhood(
                            user_id=user_id, entity_id=r, hops=h, temporal_scope=s
                        ),
                    )
            await _time(
                f"neighborhood {kind:6} deg={degree:<5} h=1 +memories",
                lambda r=root: service.neighborhood(
                    user_id=user_id, entity_id=r, include={GraphInclude.MEMORIES}
                ),
            )
        for scope in (TemporalScope.CURRENT, TemporalScope.ALL):
            await _time(
                f"overview {scope.value}",
                lambda s=scope: service.overview(user_id=user_id, temporal_scope=s),
            )
        # Evidence on a real hub edge: its asserting memory exists but has no
        # source -- an explicit NO_SOURCES answer, at graph scale.
        hub = roots["hub"][0]
        rows = await graph.read(
            "MATCH (a:Entity {id: $h, user_id: $u})-[r:RELATES_TO {user_id: $u}]->(b) "
            "RETURN b.id AS b, r.predicate AS p ORDER BY b.id LIMIT 1",
            {"h": hub, "u": user_id},
        )
        if rows:
            async with db.session() as session:
                ev_service = _service(graph, settings, session)
                await _time(
                    "evidence hub edge (all scope)",
                    lambda: ev_service.edge_evidence(
                        user_id=user_id,
                        source_entity_id=hub,
                        target_entity_id=rows[0]["b"],
                        predicate=rows[0]["p"],
                        temporal_scope=TemporalScope.ALL,
                    ),
                )
        print()
    finally:
        await _cleanup(db, graph, [user_id])


def _rel(content: str, subject: str, obj: str) -> MemoryCreate:
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [
                {"name": subject, "entity_type": "project"},
                {"name": obj, "entity_type": "technology"},
            ],
            "relationships": [{"source": subject, "predicate": "uses", "target": obj}],
        }
    )


async def bench_evidence_and_timeline(db: Database, graph: GraphClient, settings: Settings) -> None:
    user_id = f"bench-graphapi-ev-{uuid.uuid4().hex[:6]}"
    clock = {"now": NOW}
    memories = MemoryService(MemoryRepository(graph), now_fn=lambda: clock["now"])
    try:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=NOW)
            conv = await ConversationRepository(session, user_id=user_id).create(
                conversation_id=str(uuid.uuid4()), title="bench", now=NOW
            )
            msgs = [
                await MessageRepository(session, user_id=user_id).append(
                    message_id=str(uuid.uuid4()),
                    conversation_id=conv.id,
                    role=MessageRole.USER,
                    content=f"Evidence message {i}",
                    token_count=None,
                    now=NOW,
                )
                for i in range(10)
            ]

        async def make(subject: str, obj: str, n_memories: int, n_sources: int) -> None:
            for i in range(n_memories):
                created = await memories.create(
                    user_id=user_id, payload=_rel(f"{subject} uses {obj} ({i})", subject, obj)
                )
                for j in range(n_sources):
                    await memories.attach_source(
                        user_id=user_id,
                        memory_id=created.memory.id,
                        source_type=SourceType.MESSAGE,
                        reference=msgs[(i + j) % 10].id,
                        location=f"{i}:{j}",
                    )

        await make("P-one", "T-one", 1, 1)
        await make("P-many", "T-many", 10, 1)
        await make("P-sources", "T-sources", 1, 10)
        await make("P-none", "T-none", 1, 0)
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4)
        ids = {
            name: (await retrieval.find_entity_by_name(user_id=user_id, name=name)).id
            for name in (
                "P-one",
                "T-one",
                "P-many",
                "T-many",
                "P-sources",
                "T-sources",
                "P-none",
                "T-none",
            )
        }
        await graph.write(
            """
            MATCH (a:Entity {id: $a, user_id: $u}), (b:Entity {id: $b, user_id: $u})
            CREATE (a)-[:RELATES_TO {id: $rid, user_id: $u, predicate: 'depends_on',
                    status: 'active', confidence: 1.0, created_at: $t, updated_at: $t}]->(b)
            """,
            {
                "a": ids["P-none"],
                "b": ids["T-none"],
                "u": user_id,
                "rid": str(uuid.uuid4()),
                "t": NOW,
            },
        )

        print("=== Evidence (dedicated fixture; counts are per request) ===")
        cases = [
            ("no asserting memory", "P-none", "T-none", "depends_on"),
            ("one memory, no sources", "P-none", "T-none", "uses"),
            ("one memory, one source", "P-one", "T-one", "uses"),
            ("10 memories x 1 source", "P-many", "T-many", "uses"),
            ("1 memory x 10 sources", "P-sources", "T-sources", "uses"),
        ]
        for label, s, t, p in cases:
            async with db.session() as session:
                service = _service(graph, settings, session)
                reads: list[str] = []
                sqls: list[str] = []
                original = graph.read

                async def counting(q, params=None, _o=original, _reads=reads):  # noqa: ANN001, ANN202
                    _reads.append(q)
                    return await _o(q, params)

                engine = session.bind.sync_engine  # type: ignore[union-attr]

                def on_sql(*_a: object, _sqls: list = sqls, **_k: object) -> None:
                    _sqls.append("x")

                graph.read = counting  # type: ignore[method-assign]
                event.listen(engine, "before_cursor_execute", on_sql)
                try:
                    result = await _time(
                        f"evidence {label}",
                        lambda s=s, t=t, p=p, svc=service: svc.edge_evidence(
                            user_id=user_id,
                            source_entity_id=ids[s],
                            target_entity_id=ids[t],
                            predicate=p,
                        ),
                    )
                finally:
                    graph.read = original  # type: ignore[method-assign]
                    event.remove(engine, "before_cursor_execute", on_sql)
                statuses = sorted({a.evidence_status.value for a in result.assertions})
                print(
                    f"      -> assertions={len(result.assertions)} statuses={statuses} "
                    f"neo4j_reads/request={len(reads) / TRIALS:.0f} "
                    f"sql_statements/request={len(sqls) / TRIALS:.0f}"
                )

        print("\n=== Timeline (bounded at 20) ===")
        for length in (1, 5, 20, 25):
            previous = None
            last = None
            for i in range(length):
                clock["now"] = NOW + timedelta(days=i)
                created = await memories.create(
                    user_id=user_id,
                    payload=MemoryCreate.model_validate(
                        {
                            "content": f"chain {length} step {i}",
                            "entities": [{"name": f"Chain {length}", "entity_type": "project"}],
                            "supersedes": [previous] if previous else [],
                        }
                    ),
                )
                previous = last = created.memory.id
            service = _service(graph, settings)
            await _time(
                f"timeline chain length {length}",
                lambda m=last, svc=service: svc.memory_timeline(user_id=user_id, memory_id=m),
            )

        print("\n=== Canonical explorer questions: HTTP requests, old endpoints vs /graph/* ===")
        await _canonical_questions(db, graph, user_id, ids)
    finally:
        await _cleanup(db, graph, [user_id])


async def _canonical_questions(db: Database, graph: GraphClient, user_id: str, ids: dict) -> None:
    fastapi_app.dependency_overrides[get_db] = lambda: db
    fastapi_app.dependency_overrides[get_client] = lambda: graph
    # Phase 9: these request counts compare endpoint SHAPES, so identity is the
    # explicit development placeholder (never valid in production) and rate
    # limiting is off; the auth overhead is measured by eval/auth_benchmark.py.
    bench_settings = get_settings().model_copy(
        update={
            "auth_mode": "dev_header",
            "allow_user_id_header": True,
            "rate_limit_enabled": False,
        }
    )
    fastapi_app.dependency_overrides[get_app_settings] = lambda: bench_settings
    headers = {"X-User-Id": user_id}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app), base_url="http://bench"
        ) as client:
            counts: dict[str, int] = {}
            size: dict[str, int] = {}

            async def get(tag: str, url: str, **params: Any) -> Any:
                r = await client.get(url, params=params, headers=headers)
                r.raise_for_status()
                counts[tag] = counts.get(tag, 0) + 1
                size[tag] = size.get(tag, 0) + len(r.content)
                return r.json()

            p, t = ids["P-many"], ids["T-many"]
            # Q1 "What is connected to X?"
            await get("q1_old", f"/entities/{p}/graph", hops=1)
            await get("q1_new", f"/graph/entities/{p}/neighborhood")
            # Q2 "Why does the system believe X uses Y?"
            rels = await get("q2_old", f"/entities/{p}/relationships")
            memory_ids = {
                v["relationship"]["memory_id"]
                for v in rels
                if v["relationship"]["target_entity_id"] == t
                and v["relationship"]["predicate"] == "uses"
            }
            for mid in sorted(memory_ids):
                await get("q2_old", f"/memories/{mid}")
                await get("q2_old", f"/memories/{mid}/provenance")
            await get(
                "q2_new",
                "/graph/edges/evidence",
                source_entity_id=p,
                target_entity_id=t,
                predicate="uses",
            )
            # Q3 "What did X use in March?" as a drawable graph.
            await get("q3_old", f"/entities/{p}/history", as_of="2026-03-01T00:00:00Z")
            await get(
                "q3_new",
                f"/graph/entities/{p}/neighborhood",
                temporal_scope="as_of",
                as_of="2026-03-01T00:00:00Z",
            )
            for q in ("q1", "q2", "q3"):
                print(
                    f"  {q}: old={counts[q + '_old']:3} requests / {size[q + '_old']:7} B   "
                    f"new={counts[q + '_new']:3} requests / {size[q + '_new']:7} B"
                )
            print(f"  (q2 fixture: {len(memory_ids)} asserting memories x 1 source each)")
    finally:
        fastapi_app.dependency_overrides.clear()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scales", default=",".join(str(n) for n in SCALES))
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    print(
        f"Phase 8 /graph/* benchmark -- budgets nodes={settings.graph_explore_max_nodes} "
        f"edges={settings.graph_explore_max_edges} hops<={settings.graph_explore_max_hops} "
        f"trials={TRIALS}; synthetic data, one local Community instance\n"
    )
    try:
        for n in [int(x) for x in args.scales.split(",")]:
            await bench_scale(db, graph, settings, n)
        await bench_evidence_and_timeline(db, graph, settings)
    finally:
        await db.close()
        await graph.close()
    print(f"\nbudget violations across every measured slice: {budget_violations}")
    return 0 if budget_violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
