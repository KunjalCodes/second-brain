"""Background-job throughput characterization (docs/PHASE11_PLAN.md sections 6.3, 17).

Runs the REAL queue + runner code against local PostgreSQL and Neo4j with a
scripted FakeLLMClient (no network, no API key):

* extraction: N single-message conversations -> N jobs, drained by worker
  ticks; every job yields one grounded candidate, so each job performs real
  Neo4j writes and real embedding bookkeeping (fake embeddings);
* claim contention: K concurrent `claim_batch(limit=1)` calls on a queue of
  pending jobs;
* ingestion: D text documents through upload -> ingestion tick -> chunk jobs.

Everything is created under throwaway user ids and deleted afterwards.
Local numbers, not capacity claims.

    uv run python eval/job_benchmark.py --jobs 60 --claimers 10 --docs 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.documents.ingestion import run_ingestion_worker_tick  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.extraction.runner import run_worker_tick  # noqa: E402
from app.extraction.trigger import maybe_trigger_extraction  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.models.conversation import MessageRole  # noqa: E402
from app.models.extraction import (  # noqa: E402
    CandidateEntity,
    CandidateMemory,
    CandidateTemporal,
    ExtractionResult,
    Span,
)
from app.repositories.conversation_repository import ConversationRepository  # noqa: E402
from app.repositories.extraction_queue import ExtractionQueue  # noqa: E402
from app.repositories.extraction_repository import ExtractionJobRepository  # noqa: E402
from app.repositories.message_repository import MessageRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.services.document_service import DocumentService  # noqa: E402
from app.storage.local import LocalDocumentStorage  # noqa: E402

_ITEM = re.compile(r"Benchmark tool number (\d+)")
_TABLES = (
    "memory_review_candidates",
    "entity_merge_candidates",
    "memory_embeddings",
    "extraction_candidates",
    "extraction_jobs",
    "ingestion_jobs",
    "document_chunks",
    "documents",
    "messages",
    "conversations",
)


def _respond(prompt: str) -> ExtractionResult:
    """One grounded candidate per message, named after the number in it."""
    found = _ITEM.findall(prompt)
    if not found:
        return ExtractionResult(candidates=[])
    n = found[-1]
    content = f"I use Benchmark tool number {n} every day."
    name = f"Benchmark tool number {n}"
    start = content.index(name)
    return ExtractionResult(
        candidates=[
            CandidateMemory(
                statement=f"User uses {name} every day",
                memory_type="semantic",
                persistence="persistent",
                confidence=0.9,
                entities=[
                    CandidateEntity(
                        name=name,
                        entity_type="technology",
                        mention_span=Span(start=start, end=start + len(name)),
                    )
                ],
                relationships=[],
                temporal=CandidateTemporal(),
                source_message_id="placeholder",
                source_span=Span(start=0, end=len(content)),
                reasoning="benchmark fixture",
            )
        ]
    )


def _stats(xs: list[float]) -> dict[str, float | int | None]:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    return {
        "n": len(s),
        "p50_ms": round(s[len(s) // 2], 1),
        "p95_ms": round(s[min(len(s) - 1, round(0.95 * (len(s) - 1)))], 1),
        "max_ms": round(s[-1], 1),
        "mean_ms": round(statistics.fmean(s), 1),
    }


async def _make_jobs(db: Database, settings, user_id: str, n: int, *, offset: int = 0) -> None:  # noqa: ANN001
    now = datetime.now(UTC)
    for i in range(offset, offset + n):
        async with db.session() as session:
            conv = await ConversationRepository(session, user_id=user_id).create(
                conversation_id=str(uuid.uuid4()), title=f"bench {i}", now=now
            )
            msg_repo = MessageRepository(session, user_id=user_id)
            message = await msg_repo.append(
                message_id=str(uuid.uuid4()),
                conversation_id=conv.id,
                role=MessageRole.USER,
                content=f"I use Benchmark tool number {i} every day.",
                token_count=None,
                now=now,
            )
            await maybe_trigger_extraction(
                message=message,
                message_repo=msg_repo,
                job_repo=ExtractionJobRepository(session, user_id=user_id),
                settings=settings,
                now=now,
            )


async def _count_status(db: Database, user_id: str) -> dict[str, int]:
    async with db.session() as session:
        rows = await session.execute(
            text("SELECT status, count(*) FROM extraction_jobs WHERE user_id=:u GROUP BY status"),
            {"u": user_id},
        )
        return {r[0]: r[1] for r in rows}


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=60)
    p.add_argument("--batch", type=int, default=10, help="worker tick claim size")
    p.add_argument("--claimers", type=int, default=10)
    p.add_argument("--claim-jobs", type=int, default=100)
    p.add_argument("--docs", type=int, default=10)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    base = get_settings()
    settings = base.model_copy(update={"extraction_enabled": True, "extraction_window_size": 1})
    db = Database(settings)
    await db.connect()
    graph = GraphClient(settings)
    await graph.connect()
    user_ids = [f"bench-{uuid.uuid4().hex[:10]}" for _ in range(3)]
    results: dict[str, object] = {}
    try:
        for uid in user_ids:
            async with db.session() as session:
                await UserRepository(session).ensure_exists(uid, now=datetime.now(UTC))

        # -- extraction throughput -----------------------------------------------
        uid = user_ids[0]
        await _make_jobs(db, settings, uid, args.jobs)
        llm = FakeLLMClient(respond_fn=_respond)
        embeddings = FakeEmbeddingClient(dimension=settings.embedding_dimension)
        tick_ms: list[float] = []
        t0 = time.perf_counter()
        processed = 0
        while True:
            t = time.perf_counter()
            n = await run_worker_tick(
                db=db,
                graph_client=graph,
                llm_client=llm,
                embedding_client=embeddings,
                settings=settings,
                worker_id="bench",
                limit=args.batch,
            )
            if n == 0:
                break
            tick_ms.append((time.perf_counter() - t) * 1000 / n)
            processed += n
        elapsed = time.perf_counter() - t0
        results["extraction"] = {
            "jobs": processed,
            "elapsed_s": round(elapsed, 2),
            "jobs_per_min": round(processed / elapsed * 60, 1),
            "per_job_ms_by_tick": _stats(tick_ms),
            "final_status": await _count_status(db, uid),
        }
        print(json.dumps(results["extraction"]), file=sys.stderr)

        # -- claim contention -------------------------------------------------------
        uid = user_ids[1]
        await _make_jobs(db, settings, uid, args.claim_jobs, offset=100000)
        claim_ms: list[float] = []
        claimed_ids: list[str] = []

        async def claimer(k: int) -> None:
            while True:
                t = time.perf_counter()
                async with db.session() as session:
                    got = await ExtractionQueue(session).claim_batch(
                        limit=1,
                        worker_id=f"claimer-{k}",
                        now=datetime.now(UTC),
                        max_attempts=settings.extraction_max_attempts,
                    )
                claim_ms.append((time.perf_counter() - t) * 1000)
                if not got:
                    return
                claimed_ids.extend(j.id for j in got)

        await asyncio.gather(*(claimer(k) for k in range(args.claimers)))
        results["claim_contention"] = {
            "claimers": args.claimers,
            "jobs": args.claim_jobs,
            "claimed": len(claimed_ids),
            "duplicates": len(claimed_ids) - len(set(claimed_ids)),
            "claim_latency": _stats(claim_ms),
        }
        print(json.dumps(results["claim_contention"]), file=sys.stderr)

        # -- ingestion throughput -----------------------------------------------------
        uid = user_ids[2]
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalDocumentStorage(root_dir=tmp)
            for i in range(args.docs):
                body = "\n\n".join(
                    f"Section {s} of benchmark document {i}. "
                    + ("Lorem ipsum dolor sit amet. " * 60)
                    for s in range(8)
                ).encode()
                async with db.session() as session:
                    await DocumentService(session, storage, settings=settings).upload_document(
                        user_id=uid, filename=f"bench-{i}.txt", content=body
                    )
            ing_ms: list[float] = []
            t0 = time.perf_counter()
            done = 0
            while True:
                t = time.perf_counter()
                n = await run_ingestion_worker_tick(
                    db=db, storage=storage, settings=settings, worker_id="bench-ing", limit=1
                )
                if n == 0:
                    break
                ing_ms.append((time.perf_counter() - t) * 1000)
                done += n
            elapsed = time.perf_counter() - t0
        async with db.session() as session:
            chunks = (
                await session.execute(
                    text("SELECT count(*) FROM document_chunks WHERE user_id=:u"), {"u": uid}
                )
            ).scalar()
        results["ingestion"] = {
            "documents": done,
            "elapsed_s": round(elapsed, 2),
            "docs_per_min": round(done / elapsed * 60, 1) if elapsed else None,
            "per_document": _stats(ing_ms),
            "chunks_created": chunks,
        }
        print(json.dumps(results["ingestion"]), file=sys.stderr)
    finally:
        async with db.session() as session:
            for uid in user_ids:
                for table in _TABLES:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE user_id = :u"),  # noqa: S608
                        {"u": uid},
                    )
                await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        for uid in user_ids:
            await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": uid})
        await db.close()
        await graph.close()

    out = json.dumps(results, indent=2)
    if args.out:
        args.out.write_text(out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
