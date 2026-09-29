"""Deterministic memory-quality evaluation (Phase 14, M15).

    cd backend && uv run python eval/memory_quality_runner.py [--json out.json]

Runs `eval/datasets/memory_quality_v1.jsonl` through the **real** pipeline --
gate, trigger, `ExtractionRunner`, decision engine, `MemoryService`, Neo4j,
PostgreSQL, `HybridRetrievalService`, `ProvenanceService` -- with a scripted
`FakeLLMClient` and the deterministic fake embeddings. It therefore measures
the application's memory *decisions* given a fixed extraction, not the quality
of a real model's extraction (that is the opt-in real-LLM harness's job).

Task kinds (`op`):

* `pipeline`      -- messages (with stated and processing times) + scripted
                     candidates -> decisions, stored memories, statuses,
                     valid time, sources, review rows, message statuses;
* `resolution_pair` -- `is_plausible_duplicate(a, b)`;
* `intent`        -- `detect_temporal_intent(query, now)`;
* `merged_write`  -- a write naming an entity merged away earlier;
* `provenance`    -- memory -> source -> originating message, and a lost one;
* `retrieval`     -- `memory_quality_retrieval_v1.json`: Recall@K, MRR and
                     forbidden hits per configuration (signal ablations).

Every synthetic user is deleted afterwards. No number here is hardcoded.
"""

# ruff: noqa: E501 - SQL/Cypher strings and the report table are kept on one line each

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.extraction.resolution import is_plausible_duplicate  # noqa: E402
from app.extraction.runner import run_worker_tick  # noqa: E402
from app.extraction.trigger import maybe_trigger_extraction  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.models.conversation import MessageRole  # noqa: E402
from app.models.extraction import (  # noqa: E402
    CandidateEntity,
    CandidateMemory,
    CandidateRelationship,
    CandidateTemporal,
    ExtractionResult,
    Span,
)
from app.repositories.conversation_repository import ConversationRepository  # noqa: E402
from app.repositories.embedding_repository import EmbeddingRepository  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.extraction_repository import ExtractionJobRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.message_repository import MessageRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.semantic.query_understanding import detect_temporal_intent  # noqa: E402
from app.services.hybrid_retrieval_service import HybridRetrievalService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.provenance_service import ProvenanceService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from eval.metrics import reciprocal_rank  # noqa: E402

DATASET = Path(__file__).parent / "datasets" / "memory_quality_v1.jsonl"
RETRIEVAL = Path(__file__).parent / "datasets" / "memory_quality_retrieval_v1.json"
DEFAULT_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_TARGET = re.compile(r"Target message id: (\S+)")
_PG_TABLES = (
    "memory_review_candidates", "entity_merge_candidates", "memory_embeddings",
    "extraction_candidates", "extraction_jobs", "messages", "conversations",
)  # fmt: skip


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _iso(value: object) -> str | None:
    if value is None:
        return None
    native = value.to_native() if hasattr(value, "to_native") else value
    return native.astimezone(UTC).isoformat()


def _span(content: str, needle: str) -> Span | None:
    i = content.find(needle)
    return Span(start=i, end=i + len(needle)) if i >= 0 else None


def _candidate(spec: dict, message_id: str, content: str) -> CandidateMemory:
    return CandidateMemory(
        statement=spec["s"],
        memory_type=spec.get("mtype", "semantic"),
        persistence=spec["p"],
        confidence=spec.get("conf", 0.9),
        entities=[
            CandidateEntity(name=n, entity_type=t, mention_span=_span(content, n))
            for n, t in spec.get("ents", [])
        ],
        relationships=[
            CandidateRelationship(source=a, predicate=p, target=b, confidence=spec.get("conf", 0.9))
            for a, p, b in spec.get("rels", [])
        ],
        temporal=CandidateTemporal(
            is_change=spec.get("change", False),
            replaces_hint=spec.get("replaces"),
            valid_from_hint=spec.get("from_hint"),
            valid_until_hint=spec.get("until_hint"),
        ),
        source_message_id=message_id,
        source_span=Span(start=0, end=len(content)),
        reasoning="scripted evaluation candidate",
    )


class Env:
    def __init__(self, db: Database, graph: GraphClient, settings) -> None:  # noqa: ANN001
        self.db, self.graph, self.settings = db, graph, settings
        self.emb = FakeEmbeddingClient()

    async def new_user(self) -> str:
        uid = f"eval-mq-{uuid.uuid4().hex[:12]}"
        async with self.db.session() as s:
            await UserRepository(s).ensure_exists(uid, now=DEFAULT_AT)
        return uid

    async def cleanup(self, uid: str) -> None:
        async with self.db.session() as s:
            for t in _PG_TABLES:
                await s.execute(text(f"DELETE FROM {t} WHERE user_id = :u"), {"u": uid})  # noqa: S608
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await self.graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": uid})


# -- pipeline -------------------------------------------------------------------------


async def run_pipeline(env: Env, uid: str, task: dict) -> dict:
    """Returns what the stores hold afterwards (the observation)."""
    async with env.db.session() as s:
        conv = await ConversationRepository(s, user_id=uid).create(
            conversation_id=str(uuid.uuid4()), title=task["task_id"], now=DEFAULT_AT
        )
    by_target: dict[str, list[CandidateMemory]] = {}
    message_ids: list[str] = []

    def respond(prompt: str) -> ExtractionResult:
        m = _TARGET.search(prompt)
        return ExtractionResult(candidates=by_target.get(m.group(1), []) if m else [])

    llm = FakeLLMClient(respond_fn=respond)

    async def tick(at: datetime) -> None:
        for _ in range(10):  # drain every job that exists
            n = await run_worker_tick(
                db=env.db, graph_client=env.graph, llm_client=llm, embedding_client=env.emb,
                settings=env.settings, worker_id="eval-mq", limit=10, now_fn=lambda: at,
            )  # fmt: skip
            if n == 0:
                return

    burst = task.get("mode") == "burst"
    last_process = DEFAULT_AT
    for i, turn in enumerate(task["turns"]):
        at = _dt(turn.get("at")) or DEFAULT_AT + timedelta(minutes=i)
        async with env.db.session() as s:
            mrepo = MessageRepository(s, user_id=uid)
            msg = await mrepo.append(
                message_id=str(uuid.uuid4()), conversation_id=conv.id, role=MessageRole.USER,
                content=turn["content"], token_count=None, now=at,
            )  # fmt: skip
            await maybe_trigger_extraction(
                message=msg, message_repo=mrepo, job_repo=ExtractionJobRepository(s, user_id=uid),
                settings=env.settings, now=at,
            )  # fmt: skip
        message_ids.append(msg.id)
        by_target[msg.id] = [_candidate(c, msg.id, turn["content"]) for c in turn.get("cands", [])]
        process_at = _dt(turn.get("process_at")) or at
        last_process = max(last_process, process_at)
        if not burst:
            await tick(process_at)
    if burst:
        await tick(last_process)

    async with env.db.session() as s:
        cands = (
            await s.execute(
                text(
                    "SELECT c.decision, c.decision_reason, c.source_message_id FROM extraction_candidates c "
                    "JOIN messages m ON m.id = c.source_message_id "
                    "WHERE c.user_id = :u ORDER BY m.sequence, c.ordinal"
                ),
                {"u": uid},
            )
        ).all()
        statuses = [
            r[0]
            for r in await s.execute(
                text(
                    "SELECT extraction_status FROM messages WHERE conversation_id = :c ORDER BY sequence"
                ),
                {"c": conv.id},
            )
        ]
        review = (
            await s.execute(
                text("SELECT count(*) FROM memory_review_candidates WHERE user_id = :u"), {"u": uid}
            )
        ).scalar()
    mems = await env.graph.read(
        "MATCH (m:Memory {user_id: $u}) OPTIONAL MATCH (m)-[:DERIVED_FROM]->(s:Source) "
        "RETURN m.content AS content, m.status AS status, m.valid_from AS vf, m.valid_until AS vu, "
        "count(s) AS sources",
        {"u": uid},
    )
    return {
        "decisions": [c[0] for c in cands],
        "reasons": [c[1] for c in cands],
        "stored": len(mems),
        "message_statuses": statuses,
        "review": review,
        "memory": {
            m["content"]: {
                "status": m["status"],
                "valid_from": _iso(m["vf"]),
                "valid_until": _iso(m["vu"]),
                "sources": m["sources"],
            }
            for m in mems
        },  # fmt: skip
    }


def check_pipeline(expect: dict, got: dict) -> list[str]:
    problems = []
    for key in ("decisions", "stored", "message_statuses", "review"):
        if key in expect and expect[key] != got[key]:
            problems.append(f"{key}: expected {expect[key]!r}, got {got[key]!r}")
    for statement, fields in expect.get("memory", {}).items():
        actual = got["memory"].get(statement)
        if actual is None:
            problems.append(f"memory {statement!r} not stored")
            continue
        for f, v in fields.items():
            if actual.get(f) != v:
                problems.append(f"{statement!r}.{f}: expected {v!r}, got {actual.get(f)!r}")
    for statement, n in expect.get("sources", {}).items():
        actual = got["memory"].get(statement, {}).get("sources")
        if actual != n:
            problems.append(f"{statement!r}.sources: expected {n}, got {actual}")
    return problems


# -- other ops ------------------------------------------------------------------------


async def run_merged_write(env: Env, uid: str) -> list[str]:
    ms = MemoryService(MemoryRepository(env.graph), now_fn=lambda: DEFAULT_AT)

    def mc(content: str, ents: list, rels: list) -> MemoryCreate:
        return MemoryCreate.model_validate({
            "content": content,
            "entities": [{"name": n, "entity_type": t} for n, t in ents],
            "relationships": [{"source": a, "predicate": p, "target": b} for a, p, b in rels],
        })  # fmt: skip

    a = await ms.create(user_id=uid, payload=mc("Atlas uses OpenAI embeddings.",
                        [["Atlas", "project"], ["OpenAI", "company"]], [["Atlas", "uses", "OpenAI"]]))  # fmt: skip
    b = await ms.create(user_id=uid, payload=mc("Atlas was evaluated against Open AI models.",
                        [["Atlas", "project"], ["Open AI", "company"]], [["Atlas", "evaluated_against", "Open AI"]]))  # fmt: skip
    canonical = next(e.id for e in a.entities if e.name == "OpenAI")
    merged = next(e.id for e in b.entities if e.name == "Open AI")
    await EntityRepository(env.graph).merge(
        user_id=uid, entity_id_a=canonical, entity_id_b=merged, merge_id=str(uuid.uuid4()),
        new_aliases=["Open AI"], excluded_statuses=[], now=DEFAULT_AT, id_fn=lambda: str(uuid.uuid4()),
    )  # fmt: skip
    await run_pipeline(env, uid, {"task_id": "merged_write", "turns": [{
        "content": "Open AI released a faster embedding model for Atlas.",
        "cands": [{"s": "Open AI released a faster embedding model.", "p": "persistent",
                   "ents": [["Open AI", "company"], ["Atlas", "project"]],
                   "rels": [["Atlas", "uses", "Open AI"]]}],
    }]})  # fmt: skip
    rows = await env.graph.read(
        "MATCH (m:Memory {user_id: $u, content: 'Open AI released a faster embedding model.'})"
        "-[:MENTIONS]->(e:Entity) RETURN e.id AS id, e.name AS name",
        {"u": uid},
    )
    mentioned = {r["id"] for r in rows}
    problems = []
    if merged in mentioned:
        problems.append("new memory MENTIONS the merged-away entity 'Open AI'")
    if canonical not in mentioned:
        problems.append("new memory does not MENTION the canonical entity 'OpenAI'")
    # Only edges the NEW memory asserted (the pre-merge edge to "Open AI" is
    # history and stays where it was).
    edges = await env.graph.read(
        "MATCH (m:Memory {user_id: $u, content: 'Open AI released a faster embedding model.'}) "
        "MATCH (:Entity {user_id: $u})-[r:RELATES_TO {user_id: $u, memory_id: m.id}]->"
        "(:Entity {id: $merged}) RETURN count(r) AS n",
        {"u": uid, "merged": merged},
    )
    if edges and edges[0]["n"]:
        problems.append(f"{edges[0]['n']} new RELATES_TO edge(s) point at the merged-away entity")
    return problems


async def run_provenance(env: Env, uid: str, task: dict) -> list[str]:
    content = "Atlas is hosted on Render and deployed on Tuesdays."
    await run_pipeline(env, uid, {"task_id": task["task_id"], "turns": [{
        "content": content,
        "cands": [{"s": "Atlas is hosted on Render.", "p": "persistent",
                   "ents": [["Atlas", "project"], ["Render", "technology"]],
                   "rels": [["Atlas", "hosted_on", "Render"]]}],
    }]})  # fmt: skip
    mem = await env.graph.read("MATCH (m:Memory {user_id: $u}) RETURN m.id AS id", {"u": uid})
    if not mem:
        return ["no memory was stored"]
    if task.get("delete_source"):
        async with env.db.session() as s:
            for t in ("extraction_candidates", "extraction_jobs", "messages"):
                await s.execute(text(f"DELETE FROM {t} WHERE user_id = :u"), {"u": uid})  # noqa: S608
    async with env.db.session() as s:
        view = await ProvenanceService(
            s, MemoryService(MemoryRepository(env.graph))
        ).get_provenance(user_id=uid, memory_id=mem[0]["id"])
    if not view.sources:
        return ["memory has no sources"]
    src = view.sources[0]
    resolved = getattr(src, "resolved", None)
    problems = []
    if resolved is None:
        problems.append("provenance exposes no explicit resolved/missing flag")
    elif resolved != task["expect"]["resolved"]:
        problems.append(f"resolved: expected {task['expect']['resolved']}, got {resolved}")
    if task["expect"]["resolved"] and (src.message is None or src.message.content != content):
        problems.append("source does not resolve to the exact originating message")
    return problems


# -- retrieval ------------------------------------------------------------------------

CONFIGS = {
    "hybrid": {},
    "vector_only": {"enable_graph": False, "enable_keyword": False},
    "graph_only": {"enable_vector": False, "enable_keyword": False},
    "keyword_only": {"enable_vector": False, "enable_graph": False},
    "hybrid_no_recency": {"weights": {"retrieval_weight_recency": 0.0}},
    "hybrid_no_confidence": {"weights": {"retrieval_weight_confidence": 0.0}},
}


async def run_retrieval(env: Env, uid: str) -> dict:
    spec = json.loads(RETRIEVAL.read_text())
    base = datetime(2026, 1, 1, tzinfo=UTC)
    now = _dt(spec["now"])
    ids: dict[str, str] = {}
    for m in spec["memories"]:
        at = base + timedelta(days=m["day"])
        ms = MemoryService(MemoryRepository(env.graph), now_fn=lambda at=at: at)
        payload = {
            "content": m["content"],
            "entities": [{"name": n, "entity_type": t} for n, t in m["entities"]],
            "relationships": [{"source": a, "predicate": p, "target": b} for a, p, b in m["rels"]],
        }
        if "supersedes" in m:
            payload["supersedes"] = [ids[m["supersedes"]]]
        if "status" in m:
            payload["status"] = m["status"]
        created = await ms.create(user_id=uid, payload=MemoryCreate.model_validate(payload))
        ids[m["key"]] = created.memory.id
        vec = await env.emb.embed(text=m["content"])
        async with env.db.session() as s:
            await EmbeddingRepository(s, user_id=uid).upsert(
                embedding_id=str(uuid.uuid4()), memory_id=created.memory.id, content=m["content"],
                embedding=vec.vector, embedding_provider=vec.provider, embedding_model=vec.model,
                embedding_dimension=vec.dimension, now=at,
            )  # fmt: skip
    key_of = {v: k for k, v in ids.items()}
    k = spec["k"]
    out: dict[str, dict] = {}
    for name, cfg in CONFIGS.items():
        settings = env.settings.model_copy(update=cfg.get("weights", {}))
        flags = {f: cfg.get(f, True) for f in ("enable_vector", "enable_graph", "enable_keyword")}
        per_query = []
        for q in spec["queries"]:
            async with env.db.session() as s:
                hy = HybridRetrievalService(
                    session=s, memory_service=MemoryService(MemoryRepository(env.graph), now_fn=lambda: now),
                    retrieval_service=RetrievalService(EntityRepository(env.graph), max_hops=4, now_fn=lambda: now),
                    embedding_client=env.emb, settings=settings, now_fn=lambda: now, **flags,
                )  # fmt: skip
                results = await hy.search(user_id=uid, query=q["query"])
            ranked = [key_of.get(r.memory.memory.id, "?") for r in results][:k]
            expect, forbid = set(q["expect"]), set(q.get("forbid", []))
            per_query.append({
                "id": q["id"], "category": q["category"], "top": ranked,
                "recall": (len(expect & set(ranked)) / len(expect)) if expect else None,
                "rr": reciprocal_rank(ranked, expect) if expect else None,
                "forbidden_hit": bool(forbid & set(ranked)),
                "returned": len(ranked),
            })  # fmt: skip
        scored = [p for p in per_query if p["recall"] is not None]
        out[name] = {
            "recall_at_k": round(sum(p["recall"] for p in scored) / len(scored), 3),
            "mrr": round(sum(p["rr"] for p in scored) / len(scored), 3),
            "forbidden_hit_rate": round(
                sum(p["forbidden_hit"] for p in per_query) / len(per_query), 3
            ),
            "empty_expected_returned": {
                p["id"]: p["returned"] for p in per_query if p["recall"] is None
            },
            "queries": per_query,
        }
    return out


# -- main -----------------------------------------------------------------------------


async def run(settings) -> dict:  # noqa: ANN001
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    env = Env(db, graph, settings)
    tasks = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    results: list[dict] = []
    retrieval: dict = {}
    try:
        for task in tasks:
            op = task["op"]
            if op == "resolution_pair":
                got = is_plausible_duplicate(task["a"], task["b"])
                problems = (
                    [] if got == task["expect"] else [f"expected {task['expect']}, got {got}"]
                )
            elif op == "intent":
                intent = detect_temporal_intent(task["query"], now=_dt(task["now"]))
                got = {"kind": intent.temporal.value}
                if intent.as_of is not None:
                    got["as_of"] = intent.as_of.astimezone(UTC).isoformat()
                problems = [
                    f"{k}: expected {v!r}, got {got.get(k)!r}"
                    for k, v in task["expect"].items()
                    if got.get(k) != v
                ]
            else:
                uid = await env.new_user()
                try:
                    if op == "pipeline":
                        observed = await run_pipeline(env, uid, task)
                        problems = check_pipeline(task["expect"], observed)
                    elif op == "merged_write":
                        problems = await run_merged_write(env, uid)
                    elif op == "provenance":
                        problems = await run_provenance(env, uid, task)
                    elif op == "retrieval":
                        retrieval = await run_retrieval(env, uid)
                        continue
                    else:
                        raise ValueError(f"unknown op {op}")
                finally:
                    await env.cleanup(uid)
            results.append({
                "task_id": task["task_id"], "category": task["category"],
                "passed": not problems, "problems": problems,
            })  # fmt: skip
    finally:
        await db.close()
        await graph.close()
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["passed"])
    return {
        "tasks": len(results),
        "passed": sum(r["passed"] for r in results),
        "by_category": {c: f"{sum(v)}/{len(v)}" for c, v in sorted(by_cat.items())},
        "results": results,
        "retrieval": retrieval,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--json", type=Path)
    args = p.parse_args()
    settings = get_settings().model_copy(update={"extraction_enabled": True})
    report = asyncio.run(run(settings))
    print(f"memory quality: {report['passed']}/{report['tasks']} tasks passed")
    for c, v in report["by_category"].items():
        print(f"  {c:<22} {v}")
    for r in report["results"]:
        if not r["passed"]:
            print(f"  FAIL {r['task_id']}: {'; '.join(r['problems'])[:300]}")
    print("\nretrieval (k=5):  recall@k  mrr   forbidden-hit-rate  returned-when-nothing-expected")
    for name, m in report["retrieval"].items():
        print(
            f"  {name:<22} {m['recall_at_k']:<8} {m['mrr']:<5} {m['forbidden_hit_rate']:<19} {m['empty_expected_returned']}"
        )
    for q in report["retrieval"].get("hybrid", {}).get("queries", []):
        flag = " FORBIDDEN" if q["forbidden_hit"] else ""
        print(f"    hybrid {q['id']:<22} recall={q['recall']} top={q['top']}{flag}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
