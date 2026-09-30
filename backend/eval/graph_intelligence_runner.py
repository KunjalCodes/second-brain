# ruff: noqa: E501 - task tables and Cypher are kept one per line
"""Deterministic graph-intelligence evaluation (Phase 15, M16).

    cd backend && uv run python eval/graph_intelligence_runner.py [--json out.json]

Seeds one synthetic "world" for user A (and a small one for user B) directly
through `MemoryService` with fixed dates, merges one entity, adds a 200-spoke
hub, then exercises the REAL HTTP routes (`/graph/paths`, `/graph/edges/explain`,
`/graph/context`, `/entities/{id}/context`, `/memories/{id}/evolution`,
`/graph/entities/{id}/neighborhood`) through the ASGI app. No LLM is involved;
embeddings are the deterministic fake. Every synthetic row and node is deleted.
Reports pass/fail per task and category, context precision/recall, path
precision, latency and payload size -- all computed, none hardcoded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
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
from app.repositories.embedding_repository import EmbeddingRepository  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.services.hybrid_retrieval_service import HybridRetrievalService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402

DATASET = Path(__file__).parent / "datasets" / "graph_intelligence_v1.json"
BASE = datetime(2024, 1, 1, tzinfo=UTC)


class World:
    def __init__(self, db: Database, graph: GraphClient) -> None:
        self.db, self.graph = db, graph
        self.emb = FakeEmbeddingClient()
        self.memory: dict[str, str] = {}  # key -> memory id
        self.key_of: dict[str, str] = {}

    async def seed(self, uid: str, specs: list[dict], prefix: str = "") -> None:
        async with self.db.session() as s:
            await UserRepository(s).ensure_exists(uid, now=BASE)
        for m in specs:
            at = BASE + timedelta(days=m["day"])
            ms = MemoryService(MemoryRepository(self.graph), now_fn=lambda at=at: at)
            payload = {
                "content": m["content"],
                "entities": [
                    {"name": e[0], "entity_type": e[1], **({"aliases": e[2]} if len(e) > 2 else {})}
                    for e in m["entities"]
                ],
                "relationships": [{"source": a, "predicate": p, "target": b} for a, p, b in m["rels"]],
                # A stated source per memory, so provenance is testable end to end.
                "source": {"source_type": "user_statement", "reference": f"eval:{prefix}{m['key']}"},
            }
            if "supersedes" in m:
                payload["supersedes"] = [self.memory[prefix + m["supersedes"]]]
            if "status" in m:
                payload["status"] = m["status"]
            created = await ms.create(user_id=uid, payload=MemoryCreate.model_validate(payload))
            key = prefix + m["key"]
            self.memory[key], self.key_of[created.memory.id] = created.memory.id, key
            if "conflicts_with" in m:
                await ms.link_conflicting(
                    user_id=uid, memory_id_a=created.memory.id,
                    memory_id_b=self.memory[prefix + m["conflicts_with"]],
                )  # fmt: skip
            vec = await self.emb.embed(text=m["content"])
            async with self.db.session() as s:
                await EmbeddingRepository(s, user_id=uid).upsert(
                    embedding_id=str(uuid.uuid4()), memory_id=created.memory.id,
                    content=m["content"], embedding=vec.vector, embedding_provider=vec.provider,
                    embedding_model=vec.model, embedding_dimension=vec.dimension, now=at,
                )  # fmt: skip

    async def entity_id(self, uid: str, name: str) -> str:
        rows = await self.graph.read(
            "MATCH (e:Entity {user_id: $u, normalized_name: $n}) RETURN e.id AS id",
            {"u": uid, "n": " ".join(name.lower().split())},
        )
        return rows[0]["id"]

    async def cleanup(self, uid: str) -> None:
        async with self.db.session() as s:
            for t in ("memory_embeddings", "users"):
                col = "id" if t == "users" else "user_id"
                await s.execute(text(f"DELETE FROM {t} WHERE {col} = :u"), {"u": uid})  # noqa: S608
        await self.graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": uid})


async def _call(c: AsyncClient, method: str, url: str, uid: str, **kw):  # noqa: ANN202
    t0 = time.perf_counter()
    r = await c.request(method, url, headers={"X-User-Id": uid}, **kw)
    return r, (time.perf_counter() - t0) * 1000, len(r.content)


def _names(path: dict) -> list[str]:
    if not path["steps"]:
        return []
    return [path["steps"][0]["from_name"]] + [st["to_name"] for st in path["steps"]]


async def run(settings) -> dict:  # noqa: ANN001, C901, PLR0915
    spec = json.loads(DATASET.read_text())
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    world = World(db, graph)
    ua, ub = f"eval-gi-a-{uuid.uuid4().hex[:8]}", f"eval-gi-b-{uuid.uuid4().hex[:8]}"
    results: list[dict] = []
    timings: dict[str, list[float]] = defaultdict(list)
    payloads: dict[str, list[int]] = defaultdict(list)
    context_scores: list[dict] = []
    try:
        await world.seed(ua, spec["world"])
        hub = spec["hub"]
        await world.seed(ua, [
            {"key": f"hub_{i}", "day": 600, "content": f"{hub['name']} uses Spoke Tech {i}.",
             "entities": [[hub["name"], "project"], [f"Spoke Tech {i}", "technology"]],
             "rels": [[hub["name"], "uses", f"Spoke Tech {i}"]]}
            for i in range(hub["spokes"])
        ])  # fmt: skip
        canonical = await world.entity_id(ua, spec["merge"]["canonical"])
        merged = await world.entity_id(ua, spec["merge"]["merged"])
        await EntityRepository(graph).merge(
            user_id=ua, entity_id_a=canonical, entity_id_b=merged, merge_id=str(uuid.uuid4()),
            new_aliases=[spec["merge"]["merged"]], excluded_statuses=[], now=BASE,
            id_fn=lambda: str(uuid.uuid4()),
        )  # fmt: skip
        await world.seed(ub, spec["other_user_world"], prefix="b:")

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_client] = lambda: graph
        app.dependency_overrides[get_embeddings] = lambda: world.emb
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://eval") as c:
            for task in spec["tasks"]:
                op, exp, problems = task["op"], task.get("expect", {}), []
                try:
                    if op in ("path", "path_budget"):
                        params = {
                            "source_entity_id": await world.entity_id(ua, task["from"]),
                            "target_entity_id": await world.entity_id(ua, task["to"]),
                        }
                        if "max_hops" in task:
                            params["max_hops"] = task["max_hops"]
                        r, ms, size = await _call(c, "GET", "/graph/paths", ua, params=params)
                        timings["paths"].append(ms)
                        payloads["paths"].append(size)
                        body = r.json()
                        if r.status_code != 200:
                            problems.append(f"HTTP {r.status_code}: {body}")
                        elif op == "path_budget":
                            if body["explored_nodes"] > exp["explored_max"]:
                                problems.append(f"explored {body['explored_nodes']} > {exp['explored_max']}")
                        else:
                            top = body["paths"][0] if body["paths"] else None
                            if exp.get("length") is None:
                                if top is not None:
                                    problems.append(f"expected no path, got {_names(top)}")
                                if exp.get("reason") and body["reason"] != exp["reason"]:
                                    problems.append(f"reason {body['reason']!r} != {exp['reason']!r}")
                            elif top is None:
                                problems.append(f"no path (reason {body['reason']})")
                            else:
                                if top["length"] != exp["length"]:
                                    problems.append(f"length {top['length']} != {exp['length']}")
                                if "names" in exp and _names(top) != exp["names"]:
                                    problems.append(f"path {_names(top)} != {exp['names']}")
                                if "basis" in exp and top["basis"] != exp["basis"]:
                                    problems.append(f"basis {top['basis']} != {exp['basis']}")
                                preds = [sorted({r_["predicate"] for r_ in st["relationships"]}) for st in top["steps"]]
                                if "predicates" in exp and preds != exp["predicates"]:
                                    problems.append(f"predicates {preds} != {exp['predicates']}")
                                kinds = [sorted({r_["relation_kind"] for r_ in st["relationships"]}) for st in top["steps"]]
                                if "relation_kinds" in exp and kinds != exp["relation_kinds"]:
                                    problems.append(f"relation kinds {kinds} != {exp['relation_kinds']}")
                                if exp.get("no_relation_kind") and any(exp["no_relation_kind"] in k for k in kinds):
                                    problems.append(f"a {exp['no_relation_kind']} step appeared: {kinds}")
                                # every step must carry provenance (asserting memories)
                                if any(not r_["memory_ids"] for st in top["steps"] for r_ in st["relationships"]):
                                    problems.append("a step has no asserting memory")
                    elif op == "neighborhood":
                        eid = await world.entity_id(ua, task["entity"])
                        params = {"temporal_scope": task["scope"]}
                        if "as_of" in task:
                            params["as_of"] = task["as_of"]
                        r, ms, size = await _call(c, "GET", f"/graph/entities/{eid}/neighborhood", ua, params=params)
                        timings["neighborhood"].append(ms)
                        body = r.json()
                        if r.status_code != 200:
                            problems.append(f"HTTP {r.status_code}: {body}")
                        else:
                            label = {n["id"]: n["label"] for n in body["nodes"]}
                            got = {(e["predicate"], label.get(e["target"] if e["source"] == eid else e["source"]))
                                   for e in body["edges"] if e["kind"] == "relates_to"}  # fmt: skip
                            for p_, t_ in exp.get("present", []):
                                if (p_, t_) not in got:
                                    problems.append(f"missing {p_}->{t_}")
                            for p_, t_ in exp.get("absent", []):
                                if (p_, t_) in got:
                                    problems.append(f"unexpected {p_}->{t_}")
                    elif op == "entity_context":
                        eid = await world.entity_id(ua, task["entity"])
                        r, ms, size = await _call(c, "GET", f"/entities/{eid}/context", ua)
                        timings["entity_context"].append(ms)
                        payloads["entity_context"].append(size)
                        body = r.json()
                        if r.status_code != 200:
                            problems.append(f"HTTP {r.status_code}: {body}")
                        else:
                            cur = {(x["predicate"], x["other_name"]) for x in body["current_relationships"]}
                            his = {(x["predicate"], x["other_name"]) for x in body["historical_relationships"]}
                            if "current" in exp and exp["current"] == [] and cur:
                                problems.append(f"expected no current relationships, got {sorted(cur)}")
                            for p_, t_ in exp.get("current", []):
                                if (p_, t_) not in cur:
                                    problems.append(f"current missing {p_}->{t_}")
                            for p_, t_ in exp.get("historical", []):
                                if (p_, t_) not in his:
                                    problems.append(f"historical missing {p_}->{t_}")
                            for p_, t_ in exp.get("not_current", []):
                                if (p_, t_) in cur:
                                    problems.append(f"{p_}->{t_} wrongly current")
                            if "canonical" in exp and body["entity"]["name"] != exp["canonical"]:
                                problems.append(f"canonical {body['entity']['name']} != {exp['canonical']}")
                            if exp.get("resolved_from") and not body["resolved_from"]:
                                problems.append("resolved_from not reported")
                            for n_ in exp.get("merged_from", []):
                                if n_ not in {m["name"] for m in body["merged_from"]}:
                                    problems.append(f"merged_from missing {n_}")
                            if "memory_total" in exp and body["memory_total"] != exp["memory_total"]:
                                problems.append(f"memory_total {body['memory_total']} != {exp['memory_total']}")
                            if "current_count_max" in exp and len(body["current_relationships"]) > exp["current_count_max"]:
                                problems.append(f"{len(body['current_relationships'])} current > cap")
                            if exp.get("truncated") and not body["truncated_relationships"]:
                                problems.append("truncation not reported")
                    elif op == "explain":
                        params = {
                            "source_entity_id": await world.entity_id(ua, task["source"]),
                            "target_entity_id": await world.entity_id(ua, task["target"]),
                            "predicate": task["predicate"],
                            "temporal_scope": task.get("scope", "current"),
                        }
                        r, ms, size = await _call(c, "GET", "/graph/edges/explain", ua, params=params)
                        timings["explain"].append(ms)
                        payloads["explain"].append(size)
                        body = r.json()
                        if r.status_code != 200:
                            problems.append(f"HTTP {r.status_code}: {body}")
                        else:
                            if "status" in exp and body["status"] != exp["status"]:
                                problems.append(f"status {body['status']} != {exp['status']}")
                            if "state" in exp and body["temporal_state"] != exp["state"]:
                                problems.append(f"state {body['temporal_state']} != {exp['state']}")
                            if "why_contains" in exp and exp["why_contains"] not in body["why"]:
                                problems.append(f"why {body['why']!r} lacks {exp['why_contains']!r}")
                            if "basis_includes" in exp and exp["basis_includes"] not in body["basis"]:
                                problems.append(f"basis {body['basis']} lacks {exp['basis_includes']}")
                            got = {world.key_of.get(m["memory_id"]) for m in body["supporting_memories"]}
                            for k in exp.get("supporting", []):
                                if k not in got:
                                    problems.append(f"supporting memory {k} missing")
                            n_src = sum(len(m["sources"]) for m in body["supporting_memories"])
                            if n_src < exp.get("sources_min", 0):
                                problems.append(f"{n_src} sources < {exp['sources_min']}")
                    elif op == "evolution":
                        mid = world.memory[task["memory"]]
                        r, ms, size = await _call(c, "GET", f"/memories/{mid}/evolution", ua)
                        timings["evolution"].append(ms)
                        body = r.json()
                        if r.status_code != 200:
                            problems.append(f"HTTP {r.status_code}: {body}")
                        else:
                            kinds = [e["kind"] for e in body["events"]]
                            if kinds != exp["kinds"]:
                                problems.append(f"kinds {kinds} != {exp['kinds']}")
                            cur = sorted(world.key_of.get(i) for i in body["current_memory_ids"])
                            if cur != sorted(exp["current"]):
                                problems.append(f"current {cur} != {exp['current']}")
                    elif op == "context":
                        payload = {"query": task["query"]}
                        if "scope" in task:
                            payload["temporal_scope"] = task["scope"]
                        r, ms, size = await _call(c, "POST", "/graph/context", ua, json=payload)
                        timings["context"].append(ms)
                        payloads["context"].append(size)
                        body = r.json()
                        if r.status_code != 200:
                            problems.append(f"HTTP {r.status_code}: {body}")
                        else:
                            names = {n["label"] for n in body["nodes"] if n["kind"] == "entity"}
                            include, exclude = set(exp.get("include", [])), set(exp.get("exclude", []))
                            recall = len(include & names) / len(include) if include else 1.0
                            leaked = sorted(exclude & names)
                            precision = len(names & include) / len(names) if names else 0.0
                            context_scores.append({"task": task["id"], "include_recall": recall,
                                                   "exclusion_rejection": 1 - len(leaked) / len(exclude) if exclude else 1.0,
                                                   "entity_precision_vs_include": round(precision, 3),
                                                   "entities": len(names), "relationships": sum(1 for e in body["edges"] if e["kind"] == "relates_to"),
                                                   "bytes": size,
                                                   "evidence": [[world.key_of.get(e["memory_id"]), e["retrieved_via"], round(e["signals"].get("vector_similarity", 0), 3)] for e in body["evidence"]],
                                                   "construction": body["construction"]})  # fmt: skip
                            if recall < 1:
                                problems.append(f"missing {sorted(include - names)}")
                            if leaked:
                                problems.append(f"irrelevant entities kept: {leaked}")
                            if "path" in exp and not any(_names(p_) == exp["path"] for p_ in body["paths"]):
                                problems.append(f"anchor path {exp['path']} not in {[_names(p_) for p_ in body['paths']]}")
                    elif op == "search_alias":
                        async with db.session() as s:
                            ms_ = MemoryService(MemoryRepository(graph))
                            hy = HybridRetrievalService(
                                session=s, memory_service=ms_,
                                retrieval_service=RetrievalService(EntityRepository(graph), max_hops=2),
                                embedding_client=world.emb, settings=settings,
                                enable_vector=False, enable_keyword=False,
                            )  # fmt: skip
                            hits = await hy.search(user_id=ua, query=task["query"])
                        got = {world.key_of.get(h.memory.memory.id) for h in hits}
                        for k in exp["memories"]:
                            if k not in got:
                                problems.append(f"{k} not retrieved via the graph channel")
                    elif op == "isolation":
                        a_atlas = await world.entity_id(ua, "Atlas")
                        a_pg = await world.entity_id(ua, "PostgreSQL")
                        checks = [
                            ("GET", "/graph/paths", {"params": {"source_entity_id": a_atlas, "target_entity_id": a_pg}}),
                            ("GET", "/graph/edges/explain", {"params": {"source_entity_id": a_atlas, "target_entity_id": a_pg, "predicate": "uses"}}),
                            ("GET", f"/entities/{a_atlas}/context", {}),
                            ("GET", f"/memories/{world.memory['atlas_pg']}/evolution", {}),
                            ("GET", f"/graph/entities/{a_atlas}/neighborhood", {"params": {"temporal_scope": "historical"}}),
                        ]
                        for method, url, kw in checks:
                            r, _, _ = await _call(c, method, url, ub, **kw)
                            if r.status_code != 404:
                                problems.append(f"user B got {r.status_code} for A's {url}")
                        r, _, _ = await _call(c, "POST", "/graph/context", ub, json={"query": "Where does Atlas store its data?"})
                        leaked = [e["memory_id"] for e in r.json().get("evidence", []) if e["memory_id"] in world.key_of and not world.key_of[e["memory_id"]].startswith("b:")]
                        if leaked:
                            problems.append(f"B's context contains A's memories: {len(leaked)}")
                        r, _, _ = await _call(c, "POST", "/graph/context", ua, json={"query": "Which tools does Atlas use? MySQL?"})
                        if any(world.key_of.get(e["memory_id"], "").startswith("b:") for e in r.json().get("evidence", [])):
                            problems.append("A's context contains B's memory")
                except Exception as exc:  # noqa: BLE001 - reported as a failed task
                    problems.append(f"{type(exc).__name__}: {exc}")
                results.append({"id": task["id"], "category": task["category"], "passed": not problems, "problems": problems})
    finally:
        app.dependency_overrides.clear()
        for uid in (ua, ub):
            await world.cleanup(uid)
        await db.close()
        await graph.close()
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["passed"])
    path_tasks = [r for r in results if r["id"].startswith(("A", "B", "F2", "J", "Q1"))]
    return {
        "tasks": len(results),
        "passed": sum(r["passed"] for r in results),
        "by_category": {c: f"{sum(v)}/{len(v)}" for c, v in sorted(by_cat.items())},
        "path_precision": round(sum(r["passed"] for r in path_tasks) / len(path_tasks), 3),
        "context": context_scores,
        "latency_ms_median": {k: round(statistics.median(v), 1) for k, v in sorted(timings.items())},
        "payload_bytes_median": {k: int(statistics.median(v)) for k, v in sorted(payloads.items())},
        "results": results,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--json", type=Path)
    args = p.parse_args()
    report = asyncio.run(run(get_settings()))
    print(f"graph intelligence: {report['passed']}/{report['tasks']} tasks passed; path precision {report['path_precision']}")
    for c, v in report["by_category"].items():
        print(f"  {c:<24} {v}")
    for r in report["results"]:
        if not r["passed"]:
            print(f"  FAIL {r['id']}: {'; '.join(r['problems'])[:400]}")
    print("context subgraphs:")
    for x in report["context"]:
        print(f"  {x['task']:<36} include_recall={x['include_recall']:.2f} rejection={x['exclusion_rejection']:.2f} entities={x['entities']} rels={x['relationships']} bytes={x['bytes']}")
    print("latency (median ms):", report["latency_ms_median"])
    print("payload (median bytes):", report["payload_bytes_median"])
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
