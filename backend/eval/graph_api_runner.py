"""Deterministic conformance harness for the memory graph exploration API
(Phase 8, docs/PHASE8_PLAN.md section 15).

Seeds one fixed fixture graph for a synthetic user (through the unchanged
`MemoryService`, the entity-merge path, and real PostgreSQL message/document
rows for provenance), runs every task in `eval/datasets/graph_api_v1.jsonl`
against `GraphExplorationService` -- the exact service behind `/graph/*` --
and scores each one by EXACT match: set expectations must have precision
AND recall of 1.0, field expectations must be equal. No LLM, no embeddings,
no network: deterministic and zero-cost.

Every task also runs twice and the two responses must be byte-identical
(`nondeterministic_ordering` otherwise), and every returned slice must be
within its own stated budget (`budget_violation` otherwise -- a hard check,
not an average). A cross-user datum is always a critical failure.

Results are persisted through the Phase 7 `EvaluationRunTracker`
(`harness="graph_api"`, `mode="deterministic"`); only ids, counts, scores and
failure categories are stored -- never memory or document content.

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/graph_api_runner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.errors import NotFoundError  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.db.models import DocumentChunk as ChunkRow  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.models.conversation import ExtractionStatus, MessageRole  # noqa: E402
from app.models.document import format_chunk_source_location  # noqa: E402
from app.models.enums import MemoryStatus  # noqa: E402
from app.models.evaluation import EvaluationTaskStatus  # noqa: E402
from app.repositories.conversation_repository import ConversationRepository  # noqa: E402
from app.repositories.document_repository import (  # noqa: E402
    DocumentChunkRepository,
    DocumentRepository,
)
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.graph_repository import GraphRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.message_repository import MessageRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.schemas.graph import GraphInclude, GraphSlice, TemporalScope  # noqa: E402
from app.schemas.memory import MemoryCreate, MemoryUpdate  # noqa: E402
from app.services.graph_exploration_service import GraphExplorationService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.provenance_service import ProvenanceService  # noqa: E402
from app.services.retrieval_service import DEFAULT_EXCLUDED, RetrievalService  # noqa: E402
from eval.persistence import EvaluationRunTracker, TaskOutcome  # noqa: E402

DATASET = Path(__file__).resolve().parent / "datasets" / "graph_api_v1.jsonl"
DATASET_VERSION = "graph_api_v1"

JAN = datetime(2026, 1, 15, 12, tzinfo=UTC)
MAR = datetime(2026, 3, 15, 12, tzinfo=UTC)
MAY = datetime(2026, 5, 1, 12, tzinfo=UTC)
JUN = datetime(2026, 6, 15, 12, tzinfo=UTC)
INSTANTS = {"JAN": JAN, "MAR": MAR, "MAY": MAY, "JUN": JUN}

#: FK-safe order, children first (mirrors tests/integration/conftest.py).
_PG_CLEANUP = (
    ("review_audit_log", "user_id"),
    ("memory_review_candidates", "user_id"),
    ("extraction_candidates", "user_id"),
    ("extraction_jobs", "user_id"),
    ("entity_merge_candidates", "user_id"),
    ("memory_embeddings", "user_id"),
    ("ingestion_jobs", "user_id"),
    ("document_chunks", "user_id"),
    ("documents", "user_id"),
    ("messages", "user_id"),
    ("conversations", "user_id"),
    ("users", "id"),
)

#: Failure category per task category (PHASE8_PLAN.md section 15).
FAILURE_CATEGORY = {
    "temporal_current": "temporal_misclassification",
    "temporal_as_of": "temporal_misclassification",
    "temporal_all": "temporal_misclassification",
    "merged_entity": "identity_error",
    "conflicting": "identity_error",
    "duplicate": "identity_error",
    "multi_assertion": "provenance_mismatch",
    "bare_statement": "identity_error",
    "document_provenance": "provenance_mismatch",
    "hub_truncation": "truncation_flag_error",
    "incremental_expansion": "truncation_flag_error",
    "cross_user": "isolation_violation",
    "timeline": "temporal_misclassification",
}


class _Clock:
    def __init__(self) -> None:
        self.now = JAN

    def __call__(self) -> datetime:
        return self.now


def _rel(content: str, subject: str, predicate: str, obj: str, **kw: Any) -> MemoryCreate:
    types = {
        "Drawing Extractor": "project",
        "Drawing Extraction Engine": "project",
        "OCR": "concept",
        "Hub": "project",
    }
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [
                {"name": subject, "entity_type": types.get(subject, "technology")},
                {"name": obj, "entity_type": types.get(obj, "technology")},
            ],
            "relationships": [{"source": subject, "predicate": predicate, "target": obj}],
            **kw,
        }
    )


def _bare(content: str, **kw: Any) -> MemoryCreate:
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [{"name": "Drawing Extractor", "entity_type": "project"}],
            **kw,
        }
    )


async def seed(db: Database, graph: GraphClient, user_id: str, other_user_id: str) -> dict:
    """The fixed fixture graph. Returns `{"e": name->id, "m": key->id}`."""
    clock = _Clock()
    memories = MemoryService(MemoryRepository(graph), now_fn=clock)
    async with db.session() as session:
        for uid in (user_id, other_user_id):
            await UserRepository(session).ensure_exists(uid, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="graph api eval", now=JAN
        )
        message = await MessageRepository(session, user_id=user_id).append(
            message_id=str(uuid.uuid4()),
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="Drawing Extractor uses Gemini.",
            token_count=None,
            now=JAN,
        )
        document, _ = await DocumentRepository(session, user_id=user_id).create(
            document_id=str(uuid.uuid4()),
            filename="ocr-decision.md",
            content_type="text/markdown",
            size_bytes=10,
            content_hash=str(uuid.uuid4()),
            storage_key=str(uuid.uuid4()),
            now=JAN,
        )
        chunk = ChunkRow(
            id=str(uuid.uuid4()),
            document_id=document.id,
            user_id=user_id,
            ordinal=0,
            content="We switched to PaddleOCR in May.",
            content_hash="x",
            page_start=2,
            page_end=2,
            heading_context="Decision",
            char_start=0,
            char_end=32,
            extraction_status=ExtractionStatus.DONE.value,
            created_at=JAN,
        )
        await DocumentChunkRepository(session, user_id=user_id).bulk_create(chunks=[chunk])

    m: dict[str, str] = {}
    msg_source = {"source_type": "message", "reference": message.id, "location": "0:30"}
    for key, payload in {
        "easy": _rel("Drawing Extractor uses EasyOCR", "Drawing Extractor", "uses", "EasyOCR"),
        "gem1": _rel(
            "Drawing Extractor uses Gemini",
            "Drawing Extractor",
            "uses",
            "Gemini",
            source=msg_source,
        ),
        "gem2": _rel("DE relies on Gemini", "Drawing Extractor", "uses", "Gemini"),
        "ocr": _rel("EasyOCR is an OCR library", "EasyOCR", "related_to", "OCR"),
        "engine": _rel(
            "Engine depends on Python", "Drawing Extraction Engine", "depends_on", "Python"
        ),
        "bare": _bare("Drawing Extractor is my favourite project"),
        "deadline_a": _bare("Drawing Extractor deadline is March"),
    }.items():
        m[key] = (await memories.create(user_id=user_id, payload=payload)).memory.id
    # Created a day apart so every timeline below has one chronological order
    # (ties would otherwise fall back to random uuids).
    clock.now = JAN + timedelta(days=1)
    m["gem_dup"] = (
        await memories.create(
            user_id=user_id,
            payload=_rel("The extractor uses Gemini", "Drawing Extractor", "uses", "Gemini"),
        )
    ).memory.id
    m["deadline_b"] = (
        await memories.create(
            user_id=user_id,
            payload=_bare("Drawing Extractor deadline is April", status="conflicting"),
        )
    ).memory.id
    await memories.link_conflicting(
        user_id=user_id, memory_id_a=m["deadline_a"], memory_id_b=m["deadline_b"]
    )
    await memories.update(
        user_id=user_id,
        memory_id=m["deadline_a"],
        payload=MemoryUpdate(status=MemoryStatus.CONFLICTING),
    )
    await MemoryRepository(graph).confirm_duplicate(
        user_id=user_id, duplicate_id=m["gem_dup"], canonical_id=m["gem1"], now=JAN
    )
    for i in range(12):
        await memories.create(
            user_id=user_id, payload=_rel(f"Hub uses Leaf {i:02d}", "Hub", "uses", f"Leaf {i:02d}")
        )
    await memories.create(
        user_id=user_id, payload=_rel("Leaf 00 uses Twig", "Leaf 00", "uses", "Twig")
    )
    # The other user owns a same-named entity; it must never leak.
    await memories.create(
        user_id=other_user_id,
        payload=_rel("Drawing Extractor uses Secret", "Drawing Extractor", "uses", "Secret"),
    )

    clock.now = MAY
    m["paddle"] = (
        await memories.create(
            user_id=user_id,
            payload=_rel(
                "Drawing Extractor uses PaddleOCR",
                "Drawing Extractor",
                "uses",
                "PaddleOCR",
                supersedes=[m["easy"]],
                source={
                    "source_type": "document",
                    "reference": document.id,
                    "location": f"{format_chunk_source_location(chunk.id)}:0:32",
                },
            ),
        )
    ).memory.id

    retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
    names = (
        "Drawing Extractor",
        "EasyOCR",
        "Gemini",
        "PaddleOCR",
        "OCR",
        "Drawing Extraction Engine",
        "Python",
        "Hub",
        "Leaf 00",
        "Leaf 01",
        "Leaf 02",
        "Twig",
    )
    e = {n: (await retrieval.find_entity_by_name(user_id=user_id, name=n)).id for n in names}
    e["other:Drawing Extractor"] = (
        await retrieval.find_entity_by_name(user_id=other_user_id, name="Drawing Extractor")
    ).id
    await EntityRepository(graph).merge(
        user_id=user_id,
        entity_id_a=e["Drawing Extractor"],
        entity_id_b=e["Drawing Extraction Engine"],
        merge_id=str(uuid.uuid4()),
        new_aliases=["Drawing Extraction Engine"],
        excluded_statuses=DEFAULT_EXCLUDED,
        now=MAY,
        id_fn=lambda: str(uuid.uuid4()),
    )
    return {"e": e, "m": m}


async def cleanup(db: Database, graph: GraphClient, user_ids: list[str]) -> None:
    async with db.session() as session:
        for uid in user_ids:
            for table, col in _PG_CLEANUP:
                await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": uid})
    for uid in user_ids:
        await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": uid})


def _ref(value: Any, ctx: dict) -> Any:
    """`@Name` -> entity id, `#key` -> memory id, `!MAR` -> instant; recursively."""
    if isinstance(value, list):
        return [_ref(v, ctx) for v in value]
    if isinstance(value, str):
        if value.startswith("@"):
            return ctx["e"][value[1:]]
        if value.startswith("#"):
            return ctx["m"][value[1:]]
        if value.startswith("!"):
            name, _, micro = value[1:].partition("-")
            instant = INSTANTS[name]
            return instant - timedelta(microseconds=int(micro)) if micro else instant
    return value


async def _call(
    service: GraphExplorationService,
    retrieval: RetrievalService,
    task: dict,
    ctx: dict,
    user_id: str,
) -> Any:
    op = task["op"]
    p = {k: _ref(v, ctx) for k, v in task.get("params", {}).items()}
    if "temporal_scope" in p:
        p["temporal_scope"] = TemporalScope(p["temporal_scope"])
    if "include" in p:
        p["include"] = {GraphInclude(i) for i in p["include"]}
    if op == "neighborhood":
        return await service.neighborhood(user_id=user_id, **p)
    if op == "overview":
        return await service.overview(user_id=user_id, **p)
    if op == "evidence":
        return await service.edge_evidence(user_id=user_id, **p)
    if op == "timeline":
        return await service.memory_timeline(user_id=user_id, **p)
    if op == "search":
        return await retrieval.search_entities(user_id=user_id, limit=25, offset=0, **p)
    raise ValueError(f"unknown op {op!r}")


def _names(ctx: dict) -> dict[str, str]:
    return {v: k for k, v in ctx["e"].items()} | {v: f"#{k}" for k, v in ctx["m"].items()}


def _prf(expected: set, got: set) -> tuple[float, float]:
    tp = len(expected & got)
    precision = tp / len(got) if got else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else 1.0
    return precision, recall


def score(task: dict, result: Any, ctx: dict) -> tuple[bool, dict, list[str]]:
    """Returns (passed, metrics, failure_notes). Notes carry names/keys only."""
    ex = task["expect"]
    notes: list[str] = []
    metrics: dict[str, Any] = {}
    names = _names(ctx)
    precisions: list[float] = []
    recalls: list[float] = []

    def check_set(label: str, expected: set, got: set) -> None:
        p, r = _prf(expected, got)
        precisions.append(p)
        recalls.append(r)
        if expected != got:
            notes.append(
                f"{label}: missing={sorted(expected - got)} extra={sorted(got - expected)}"
            )

    if ex.get("not_found"):
        ok = result == "not_found"
        return (
            ok,
            {"precision": 1.0 if ok else 0.0, "recall": 1.0 if ok else 0.0},
            ([] if ok else ["expected 404, got data"]),
        )
    if result == "not_found":
        return False, {"precision": 0.0, "recall": 0.0}, ["unexpected 404"]

    if task["op"] == "search":
        check_set("search_names", set(ex["search_names"]), {x.name for x in result})
    elif task["op"] == "evidence":
        if "assertion_memories" in ex:
            check_set(
                "assertion_memories",
                set(ex["assertion_memories"]),
                {names.get(a.memory_id, a.memory_id) for a in result.assertions},
            )
        if "evidence_available" in ex and result.evidence_available != ex["evidence_available"]:
            notes.append("evidence_available mismatch")
        if "document" in ex:
            docs = [
                s
                for a in result.assertions
                if a.memory
                for s in a.memory.sources
                if s.document is not None
            ]
            got = {
                (
                    d.document.filename,
                    d.document_chunk.page_start if d.document_chunk else None,
                    d.document_chunk.heading_context if d.document_chunk else None,
                )
                for d in docs
            }
            want = {(ex["document"]["filename"], ex["document"]["page"], ex["document"]["heading"])}
            check_set("document", want, got)
        if "message_sources" in ex:
            got = sum(
                1 for a in result.assertions if a.memory for s in a.memory.sources if s.message_id
            )
            if got != ex["message_sources"]:
                notes.append(f"message_sources {got} != {ex['message_sources']}")
    else:
        s: GraphSlice = result
        metrics["node_count"] = len(s.nodes)
        metrics["edge_count"] = len(s.edges)
        entity_labels = [n.label for n in s.nodes if n.kind.value == "entity"]
        if "entity_labels" in ex:
            check_set("entity_labels", set(ex["entity_labels"]), set(entity_labels))
        if "labels_in_order" in ex and entity_labels != ex["labels_in_order"]:
            notes.append(f"order {entity_labels} != {ex['labels_in_order']}")
        for absent in ex.get("absent_labels", []):
            # Exact for entity names ("OCR" must not match "PaddleOCR"); a
            # substring check only on memory labels, to catch leaked content.
            if absent in entity_labels or any(
                absent in n.label for n in s.nodes if n.kind.value != "entity"
            ):
                notes.append(f"leaked/absent-violated: {absent}")
        if "relates_to" in ex:
            got = {
                (names.get(e.source, e.source), e.predicate, names.get(e.target, e.target))
                for e in s.edges
                if e.kind.value == "relates_to"
            }
            check_set("relates_to", {tuple(t) for t in ex["relates_to"]}, got)
        for triple, state in ex.get("edge_states", {}).items():
            src, pred, tgt = triple.split("|")
            got = [
                e.temporal_state.value
                for e in s.edges
                if e.kind.value == "relates_to"
                and names.get(e.source) == src
                and e.predicate == pred
                and names.get(e.target) == tgt
            ]
            if got != [state]:
                notes.append(f"state {triple}: {got} != [{state}]")
        for triple, want in ex.get("edge_memories", {}).items():
            src, pred, tgt = triple.split("|")
            edge = next(
                (
                    e
                    for e in s.edges
                    if names.get(e.source) == src
                    and e.predicate == pred
                    and names.get(e.target) == tgt
                ),
                None,
            )
            got = {names.get(i, i) for i in (edge.memory_ids or [])} if edge else set()
            check_set(f"edge_memories {triple}", set(want["memory_ids"]), got)
            if edge and edge.assertion_count != want["assertion_count"]:
                notes.append(f"assertion_count {edge.assertion_count} != {want['assertion_count']}")
        memory_nodes = {names.get(n.id, n.id) for n in s.nodes if n.kind.value == "memory"}
        for key in ex.get("memory_nodes_include", []):
            if key not in memory_nodes:
                notes.append(f"memory missing: {key}")
        for key in ex.get("memory_nodes_exclude", []):
            if key in memory_nodes:
                notes.append(f"memory present: {key}")
        for kind, a, b in ex.get("links_include", []):
            if not any(
                e.kind.value == kind and {names.get(e.source), names.get(e.target)} == {a, b}
                for e in s.edges
            ):
                notes.append(f"link missing: {kind} {a} {b}")
        if "timeline" in ex:
            got = [[names.get(n.id, n.id), n.status.value] for n in s.nodes]
            if got != ex["timeline"]:
                notes.append(f"timeline {got} != {ex['timeline']}")
        for fld in ("truncated_nodes", "truncated_edges", "redirect_depth"):
            if fld in ex and getattr(s, fld) != ex[fld]:
                notes.append(f"{fld} {getattr(s, fld)} != {ex[fld]}")
        if "node_count" in ex and len(s.nodes) != ex["node_count"]:
            notes.append(f"node_count {len(s.nodes)} != {ex['node_count']}")
        for fld in ("root", "resolved_from"):
            if fld in ex and getattr(s, fld) != _ref(ex[fld], ctx):
                notes.append(f"{fld} mismatch")
        if "empty" in ex and ex["empty"] != (not s.nodes and not s.edges):
            notes.append("empty mismatch")
        # Hard budget check on every slice.
        if len(s.nodes) > s.budget.max_nodes or (
            s.budget.max_edges is not None and len(s.edges) > s.budget.max_edges
        ):
            notes.append("budget_violation")

    metrics["precision"] = min(precisions) if precisions else (0.0 if notes else 1.0)
    metrics["recall"] = min(recalls) if recalls else (0.0 if notes else 1.0)
    return not notes, metrics, notes


def load_tasks() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


async def run(
    db: Database, graph: GraphClient, settings: Settings, *, persist: bool = True
) -> dict:
    tasks = load_tasks()
    user_id = f"eval-graph-{uuid.uuid4().hex[:10]}"
    other_user_id = f"eval-graph-other-{uuid.uuid4().hex[:10]}"
    tracker = EvaluationRunTracker(db) if persist else None
    task_ids = [t["task_id"] for t in tasks]
    run_id = None
    if tracker is not None:
        run_id = await tracker.start_or_resume(
            harness="graph_api",
            dataset_version=DATASET_VERSION,
            mode="deterministic",
            task_ids=task_ids,
            config={
                "graph_explore_max_nodes": settings.graph_explore_max_nodes,
                "graph_explore_max_edges": settings.graph_explore_max_edges,
                "graph_explore_max_hops": settings.graph_explore_max_hops,
            },
        )
    done = await tracker.completed_task_ids(run_id=run_id) if tracker and run_id else set()

    results: list[dict] = []
    try:
        ctx = await seed(db, graph, user_id, other_user_id)
        clock = _Clock()
        clock.now = JUN
        for task in tasks:
            if task["task_id"] in done:
                continue
            as_user = other_user_id if task.get("as_other_user") else user_id
            async with db.session() as session:
                memory = MemoryService(MemoryRepository(graph), now_fn=clock)
                retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
                service = GraphExplorationService(
                    repository=GraphRepository(graph),
                    retrieval=retrieval,
                    memory=memory,
                    provenance=ProvenanceService(session, memory),
                    settings=settings.model_copy(update=task.get("settings", {})),
                    now_fn=clock,
                )
                outputs = []
                start = time.perf_counter()
                for _ in range(2):
                    try:
                        out = await _call(service, retrieval, task, ctx, as_user)
                    except NotFoundError:
                        out = "not_found"
                    outputs.append(out)
                latency_ms = (time.perf_counter() - start) * 1000 / 2
            passed, metrics, notes = score(task, outputs[0], ctx)

            def dump(o: Any) -> str:
                if isinstance(o, list):
                    return json.dumps([x.model_dump(mode="json") for x in o])
                return o if isinstance(o, str) else o.model_dump_json()

            if dump(outputs[0]) != dump(outputs[1]):
                passed = False
                notes.append("nondeterministic_ordering")
            category = task["category"]
            failure = None
            if not passed:
                failure = (
                    "budget_violation"
                    if "budget_violation" in notes
                    else (
                        "nondeterministic_ordering"
                        if "nondeterministic_ordering" in notes
                        else FAILURE_CATEGORY[category]
                    )
                )
            metrics["latency_ms"] = round(latency_ms, 2)
            if failure:
                metrics["failure_category"] = failure
            results.append(
                {
                    "task_id": task["task_id"],
                    "category": category,
                    "passed": passed,
                    "notes": notes,
                    **metrics,
                }
            )
            if tracker is not None and run_id is not None:
                await tracker.record_task(
                    run_id=run_id,
                    task_id=task["task_id"],
                    category=category,
                    outcome=TaskOutcome(
                        status=EvaluationTaskStatus.COMPLETED,
                        passed=passed,
                        metrics=metrics,
                        error_detail="; ".join(notes)[:1000] if notes else None,
                    ),
                )
    finally:
        await cleanup(db, graph, [user_id, other_user_id])

    summary = await tracker.finalize(run_id=run_id, task_ids=task_ids) if tracker and run_id else {}
    return {"run_id": run_id, "results": results, "summary": summary}


async def main() -> int:
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    try:
        out = await run(db, graph, settings)
    finally:
        await db.close()
        await graph.close()
    results = out["results"]
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['task_id']:<40} {r['latency_ms']:8.1f}ms  {'; '.join(r['notes'])}")
    print()
    for cat, flags in sorted(by_cat.items()):
        print(f"  {cat:<24} {sum(flags)}/{len(flags)}")
    passed = sum(r["passed"] for r in results)
    print(
        f"\ngraph_api conformance: {passed}/{len(results)} tasks passed  (run_id={out['run_id']})"
    )
    critical = [r for r in results if r["category"] == "cross_user" and not r["passed"]]
    if critical:
        print(f"CRITICAL: {len(critical)} isolation failure(s)")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
