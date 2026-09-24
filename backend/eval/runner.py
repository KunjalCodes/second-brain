"""Deterministic evaluation harness (docs/EVALUATION.md, docs/PHASE2_PLAN.md section 15).

Runs the gold dataset (`eval/datasets/extraction_v1.jsonl`) through the **real**
extraction pipeline -- `ExtractionRunner`, real Neo4j, real PostgreSQL -- using a
`FakeLLMClient` scripted to return each scenario's `expected_candidates` verbatim.

This is deliberately the **deterministic** harness (docs/PHASE2_PLAN.md section 11):
it measures whether the decision engine, grounding and persistence layers reach the
right outcome *given* a correct extraction, using the same production code path the
integration tests exercise. It does **not** measure extraction quality itself --
whether a real model would have produced `expected_candidates` in the first place.
That is the **model-quality** harness's job (Step 2.12+, opt-in, costs money,
compares a real provider's output against this same gold set) and is out of scope
for this phase.

No fabricated numbers: every figure printed is computed from an actual run against
real databases, never hardcoded (docs/EVALUATION.md section 20).

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/runner.py
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
from app.repositories.extraction_repository import (  # noqa: E402
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.repositories.message_repository import MessageRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from eval.metrics import ScenarioResult, build_report  # noqa: E402

DATASET_PATH = Path(__file__).parent / "datasets" / "extraction_v1.jsonl"


def _span(content: str, needle: str) -> Span | None:
    if needle not in content:
        return None
    start = content.index(needle)
    return Span(start=start, end=start + len(needle))


def _build_candidate(spec: dict, *, message_id: str, message_content: str) -> CandidateMemory:
    entities = []
    for e in spec.get("entities", []):
        entities.append(
            CandidateEntity(
                name=e["name"],
                entity_type=e["entity_type"],
                mention_span=_span(message_content, e["name"]),
            )
        )
    relationships = [
        CandidateRelationship(source=r["source"], predicate=r["predicate"], target=r["target"])
        for r in spec.get("relationships", [])
    ]
    temporal_spec = spec.get("temporal", {})
    return CandidateMemory(
        statement=spec.get("statement", message_content[:200]),
        memory_type=spec.get("memory_type", "semantic"),
        persistence=spec["persistence"],
        confidence=spec.get("confidence", 0.9),
        entities=entities,
        relationships=relationships,
        temporal=CandidateTemporal(
            is_change=temporal_spec.get("is_change", False),
            replaces_hint=temporal_spec.get("replaces_hint"),
        ),
        source_message_id=message_id,
        source_span=Span(start=0, end=len(message_content)),
        reasoning="gold fixture",
    )


async def run_scenario(db: Database, graph: GraphClient, settings, spec: dict) -> ScenarioResult:
    user_id = f"eval-{uuid.uuid4()}"
    now = datetime.now(UTC)

    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=now)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title=spec["id"], now=now
        )

    message_ids: list[str] = []
    for i, turn in enumerate(spec["conversation"]):
        async with db.session() as session:
            msg_repo = MessageRepository(session, user_id=user_id)
            job_repo = ExtractionJobRepository(session, user_id=user_id)
            message = await msg_repo.append(
                message_id=str(uuid.uuid4()), conversation_id=conv.id,
                role=MessageRole(turn["role"]), content=turn["content"],
                token_count=None, now=now + timedelta(seconds=i),
            )
            message_ids.append(message.id)
            await maybe_trigger_extraction(
                message=message, message_repo=msg_repo, job_repo=job_repo,
                settings=settings, now=now + timedelta(seconds=i),
            )

        specs_for_turn = spec["candidates_by_index"].get(str(i), [])
        candidates = [
            _build_candidate(c, message_id=message.id, message_content=message.content)
            for c in specs_for_turn
        ]
        llm = FakeLLMClient(response=ExtractionResult(candidates=candidates))
        await run_worker_tick(
            db=db, graph_client=graph, llm_client=llm, embedding_client=FakeEmbeddingClient(),
            settings=settings, worker_id=f"eval-{spec['id']}-{i}", limit=5,
            now_fn=lambda: now + timedelta(seconds=i),  # noqa: B023 - each tick used immediately
        )

    target_message_id = message_ids[spec["target_message_index"]]
    async with db.session() as session:
        job_repo = ExtractionJobRepository(session, user_id=user_id)
        jobs = await job_repo.list_recent(limit=len(spec["conversation"]), offset=0)
        target_job = next(j for j in jobs if j.target_message_id == target_message_id)
        records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
            extraction_job_id=target_job.id
        )

    actual_decisions = [r.decision.value for r in records]
    passed = actual_decisions == spec["expected_decisions"]
    # `decision_reason` (written by the grounding check, the domain-validation
    # catch in `_apply_candidate`, or the decision engine itself) is what tells a
    # human whether a mismatch came from grounding, persistence, or a genuine
    # decision-engine classification difference (docs/PHASE2_HARDENING.md section
    # 11) -- extraction *quality* itself is out of scope for this deterministic
    # harness, since candidates are scripted, not model-produced.
    reasons = "; ".join(f"{r.decision.value} ({r.decision_reason})" for r in records)

    # Cleanup -- eval runs must not leave data behind, matching the integration
    # test fixtures' contract.
    async with db.session() as session:
        for table in (
            "extraction_candidates", "extraction_jobs", "entity_merge_candidates",
            "memory_embeddings", "messages", "conversations", "users",
        ):
            from sqlalchemy import text

            col = "id" if table == "users" else "user_id"
            await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": user_id})
    await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": user_id})

    return ScenarioResult(
        scenario_id=spec["id"],
        category=spec["category"],
        expected_decisions=spec["expected_decisions"],
        actual_decisions=actual_decisions,
        passed=passed,
        detail="" if passed else (reasons or "no candidates were recorded"),
    )


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

    results = []
    for spec in specs:
        result = await run_scenario(db, graph, settings, spec)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.scenario_id:35} expected={result.expected_decisions} "
              f"actual={result.actual_decisions}")
        results.append(result)

    await db.close()
    await graph.close()

    report = build_report(results)
    print()
    print(report.summary())
    return 0 if report.decision_accuracy == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
