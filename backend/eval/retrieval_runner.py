"""Deterministic retrieval evaluation harness (PHASE3_PLAN.md section 16).

Runs the gold dataset (`eval/datasets/retrieval_v1.jsonl`) through the **real**
hybrid pipeline -- `HybridRetrievalService`, real Neo4j, real PostgreSQL -- using
`FakeEmbeddingClient` so no API key or network access is needed, mirroring
`eval/runner.py`'s own deterministic-harness discipline exactly.

Compares three configurations per scenario:

    graph-only   (vector and keyword channels disabled)
    vector-only  (graph and keyword channels disabled)
    hybrid       (all three channels enabled -- the real, deployed configuration)

This is deliberately scoped to *retrieval* correctness -- Recall@K, Precision@K,
MRR, and temporal accuracy -- not answer quality: there is no agent yet to
generate an answer from retrieved context (that is Phase 5). See
PHASE3_PLAN.md section 16 for the explicit scoping rationale.

No fabricated numbers: every figure printed is computed from an actual run
against real databases (EVALUATION.md section 20).

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/retrieval_runner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.repositories.embedding_repository import EmbeddingRepository  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.services.hybrid_retrieval_service import HybridRetrievalService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from eval.metrics import (  # noqa: E402
    RetrievalScenarioResult,
    build_retrieval_report,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

DATASET_PATH = Path(__file__).parent / "datasets" / "retrieval_v1.jsonl"
BASE_DATE = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_memories(db, graph, embedding_client, user_id: str, specs: list[dict]) -> list[str]:  # noqa: ANN001
    """Create every memory directly via `MemoryService` (this evaluates
    retrieval, not extraction) and embed each one. Returns memory ids in
    dataset order.
    """
    memory_ids: list[str] = []
    for spec in specs:
        now = BASE_DATE + timedelta(days=spec.get("offset_days", 0))
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda n=now: n)
        payload: dict = {"content": spec["content"], "entities": spec.get("entities", [])}
        if spec.get("relationships"):
            payload["relationships"] = spec["relationships"]
        if "supersedes_index" in spec:
            payload["supersedes"] = [memory_ids[spec["supersedes_index"]]]
        if "status" in spec:
            payload["status"] = spec["status"]
        created = await memory_service.create(
            user_id=user_id, payload=MemoryCreate.model_validate(payload)
        )
        memory_ids.append(created.memory.id)

        async with db.session() as session:
            result = await embedding_client.embed(text=spec["content"])
            await EmbeddingRepository(session, user_id=user_id).upsert(
                embedding_id=str(uuid.uuid4()),
                memory_id=created.memory.id,
                content=spec["content"],
                embedding=result.vector,
                embedding_provider=result.provider,
                embedding_model=result.model,
                embedding_dimension=result.dimension,
                now=now,
            )
    return memory_ids


async def _run_configuration(
    db,
    graph,
    embedding_client,
    settings,
    user_id: str,
    query: str,
    *,  # noqa: ANN001
    enable_vector: bool,
    enable_graph: bool,
    enable_keyword: bool,
    now: datetime,
) -> list[str]:
    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: now)
    retrieval_service = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=lambda: now)
    async with db.session() as session:
        hybrid = HybridRetrievalService(
            session=session,
            memory_service=memory_service,
            retrieval_service=retrieval_service,
            embedding_client=embedding_client,
            settings=settings,
            now_fn=lambda: now,
            enable_vector=enable_vector,
            enable_graph=enable_graph,
            enable_keyword=enable_keyword,
        )
        results = await hybrid.search(user_id=user_id, query=query)
    return [r.memory.memory.id for r in results]


async def run_scenario(
    db, graph, embedding_client, settings, spec: dict
) -> dict[str, RetrievalScenarioResult]:  # noqa: ANN001
    user_id = f"eval-retrieval-{uuid.uuid4()}"
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=BASE_DATE)

    memory_ids = await _seed_memories(db, graph, embedding_client, user_id, spec["memories"])
    expected = {memory_ids[i] for i in spec["expected_relevant_indices"]}
    now = BASE_DATE + timedelta(
        days=max((m.get("offset_days", 0) for m in spec["memories"]), default=0)
    )
    # A scenario naming an explicit hop count (the 2hop/3hop categories) needs
    # the graph channel actually configured for that many hops -- the field is
    # otherwise absent and `settings`'s own default/env value governs, exactly
    # like every other scenario.
    scenario_settings = (
        settings.model_copy(update={"retrieval_graph_hops": spec["hops"]})
        if "hops" in spec
        else settings
    )

    configurations = {
        "graph_only": {"enable_vector": False, "enable_graph": True, "enable_keyword": False},
        "vector_only": {"enable_vector": True, "enable_graph": False, "enable_keyword": False},
        "hybrid": {"enable_vector": True, "enable_graph": True, "enable_keyword": True},
    }
    results: dict[str, RetrievalScenarioResult] = {}
    for config_name, flags in configurations.items():
        retrieved = await _run_configuration(
            db, graph, embedding_client, scenario_settings, user_id, spec["query"], now=now, **flags
        )
        results[config_name] = RetrievalScenarioResult(
            scenario_id=spec["id"],
            category=spec["category"],
            retrieved=retrieved,
            expected=expected,
            recall_at_k=recall_at_k(retrieved, expected),
            precision_at_k=precision_at_k(retrieved, expected),
            reciprocal_rank=reciprocal_rank(retrieved, expected),
            detail=f"expected={sorted(expected)} retrieved={retrieved}",
        )

    # Cleanup -- eval runs must not leave data behind.
    async with db.session() as session:
        from sqlalchemy import text

        for table in ("memory_embeddings", "users"):
            col = "id" if table == "users" else "user_id"
            await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": user_id})
    await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": user_id})

    return results


async def main() -> int:
    settings = get_settings()
    if not settings.neo4j_configured or not settings.database_configured:
        print(
            "ERROR: this harness runs the real pipeline against real Neo4j and "
            "PostgreSQL. Set NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD and "
            "DATABASE_URL (see docs/API.md).",
            file=sys.stderr,
        )
        return 2

    specs = [json.loads(line) for line in DATASET_PATH.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(specs)} scenarios from {DATASET_PATH.relative_to(Path.cwd())}\n")

    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    embedding_client = FakeEmbeddingClient(dimension=settings.embedding_dimension)

    per_config: dict[str, list[RetrievalScenarioResult]] = {
        "graph_only": [],
        "vector_only": [],
        "hybrid": [],
    }
    for spec in specs:
        results = await run_scenario(db, graph, embedding_client, settings, spec)
        for config_name, result in results.items():
            per_config[config_name].append(result)
        print(
            f"  {spec['id']:22} ({spec['category']:14}) "
            f"graph_only={results['graph_only'].recall_at_k:.0%} "
            f"vector_only={results['vector_only'].recall_at_k:.0%} "
            f"hybrid={results['hybrid'].recall_at_k:.0%}"
        )

    await db.close()
    await graph.close()

    print()
    for config_name, results in per_config.items():
        print(f"=== {config_name} ===")
        print(build_retrieval_report(results).summary())
        print()

    hybrid_report = build_retrieval_report(per_config["hybrid"])
    return 0 if hybrid_report.mean_recall_at_k == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
