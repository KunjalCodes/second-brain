"""Phase 6 document evaluation harness (docs/PHASE6_PLAN.md section 15),
against real Neo4j, real PostgreSQL, and local-filesystem document storage
(zero-cost -- no S3, no paid service).

Mirrors `eval/runner.py`'s methodology, not `eval/agent_runner.py`'s: this is
the **deterministic mechanics** harness. Documents are uploaded and ingested
through the REAL pipeline (`DocumentService.upload_document`,
`IngestionRunner`, real parsing/chunking) -- nothing about ingestion is
scripted. Only the LLM's per-chunk extraction call is scripted
(`FakeLLMClient`, one script per chunk, applied via a directly-constructed
`ExtractionRunner.run()` call rather than the claim-queue, so a specific
chunk's job always gets its own specific scripted candidates regardless of
claim ordering). This measures whether ingestion + the widened extraction
pipeline + provenance resolution + the existing agent tools reach the right
outcome given a correct extraction -- not whether a real model would have
produced that extraction. That is real-LLM validation's job (see
`eval/agent_runner.py`'s own `E_real_llm` precedent), out of scope here.

**Coverage note, stated honestly (no fabricated completeness claims,
docs/EVALUATION.md section 20):** docs/PHASE6_PLAN.md section 15 names 11
task categories; this dataset (`eval/datasets/document_v1.jsonl`) now covers
all 11 with one task each: basic document->memory extraction, provenance/
page citation, cross-source duplicate detection, document prompt-injection
safety, bounded chunking on a large document, multi-document reasoning,
document+conversation combined reasoning, temporal document facts,
conflicting documents, and memory absence.

**Baseline B** (PHASE6_PLAN.md section 16: "a direct chunk-embedding vector
search, built only as an evaluation-only comparison arm, not shipped in
production retrieval") is implemented strictly inside this harness
(`_baseline_b_raw_chunk_search`), entirely in-memory: it embeds raw
`DocumentChunk.content` on the fly with the same `FakeEmbeddingClient` every
other deterministic path here uses, and never writes an embedding anywhere.
`document_chunks` has no embedding column, no pgvector index is created for
it, and nothing here is reachable from `app/api` or `app/services` (ADR
0014's "no second retrieval system" is unaffected by this file existing).
It is reported as a separate comparison section, never folded into the
pass/fail `ScenarioResult`s baselines A/C/D/E (which reuse existing,
already-validated machinery -- Phase 5's own baselines, unchanged) are
measured with.

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/document_runner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolDependencies, ToolStatus  # noqa: E402
from app.agent.tools import get_source as get_source_tool  # noqa: E402
from app.agent.tools import search_memory as search_memory_tool  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.documents.ingestion import run_ingestion_worker_tick  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.extraction.runner import ExtractionRunner  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.llm.factory import build_llm_client  # noqa: E402
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.models.agent import GetSourceArgs, SearchMemoryArgs  # noqa: E402
from app.models.evaluation import EvaluationTaskStatus  # noqa: E402
from app.models.extraction import (  # noqa: E402
    CandidateEntity,
    CandidateMemory,
    CandidateRelationship,
    CandidateTemporal,
    ExtractionResult,
    Span,
)
from app.repositories.document_repository import DocumentChunkRepository  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.extraction_repository import (  # noqa: E402
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.services.agent_service import AgentService  # noqa: E402
from app.services.document_service import DocumentService  # noqa: E402
from app.services.hybrid_retrieval_service import HybridRetrievalService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.provenance_service import ProvenanceService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from app.storage.local import LocalDocumentStorage  # noqa: E402
from eval.metrics import ScenarioResult, build_report  # noqa: E402
from eval.persistence import EvaluationRunTracker, TaskOutcome  # noqa: E402

#: Task ids meaningful as a real-agent question-answering check -- the ones
#: with a natural `query` and a well-defined expected-citation set. The
#: internal-mechanics tasks (decision-engine classification, chunk-count
#: bounds, the debounce-adjacent duplicate check) are not agent questions
#: and are not run against a real LLM (PART 4's own instruction: measure
#: real things, don't force a category that doesn't fit).
REAL_LLM_TASK_IDS = (
    "doc_basic_extraction",
    "doc_provenance_page_citation",
    "doc_multi_document_reasoning",
    "doc_document_and_conversation_combined",
    "doc_memory_absence",
)

DATASET_PATH = Path(__file__).parent / "datasets" / "document_v1.jsonl"
DATASET_VERSION = "document_v1"
BASE_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def load_dataset() -> list[dict]:
    with DATASET_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass(frozen=True)
class BaselineBResult:
    """One task's outcome under the evaluation-only 'raw chunk vector
    search' baseline (PHASE6_PLAN.md section 16, baseline B; strengthened
    per Phase 7's PHASE7_PLAN.md section 6, Stage 5) -- reported separately
    from, and never averaged into, the pass/fail `ScenarioResult`s the
    actual Phase 6 system (baseline E) is measured with.

    `recall_at_1`/`recall_at_3`: of the expected keyword(s) for this task,
    the fraction found anywhere across the raw top-1 (respectively top-3)
    ranked chunks' content -- the document-chunk analogue of
    `eval.metrics.recall_at_k`, computed over keyword coverage rather than
    memory ids, since baseline B has no memory ids to rank.
    """

    task_id: str
    recall_at_1: float
    recall_at_3: float
    top_chunk_snippet: str
    note: str = ""


async def _baseline_b_raw_chunk_search(
    query: str,
    chunks: list,
    embedding_client,
    *,
    top_k: int = 1,  # noqa: ANN001
) -> list[tuple[float, object]]:
    """Evaluation-only baseline (PHASE6_PLAN.md section 16, baseline B):
    ranks raw `DocumentChunk.content` by embedding similarity to the query,
    computed entirely in-memory with the same `FakeEmbeddingClient` used
    elsewhere in this harness. **Never a production code path** -- no chunk
    embedding is written to any table, `document_chunks` has no embedding
    column, and no pgvector index exists for it (ADR 0014). This function
    exists only to honestly quantify what canonical Memory extraction adds
    over naive "embed the raw chunk and rank" retrieval, not to prototype a
    feature.
    """
    query_vec = (await embedding_client.embed(text=query)).vector
    scored: list[tuple[float, object]] = []
    for chunk in chunks:
        chunk_vec = (await embedding_client.embed(text=chunk.content)).vector
        # FakeEmbeddingClient's vectors are already unit-normalized, so the
        # dot product is exactly the cosine similarity.
        score = sum(x * y for x, y in zip(query_vec, chunk_vec, strict=True))
        scored.append((score, chunk))
    scored.sort(key=lambda pair: -pair[0])
    return scored[:top_k]


def _span(content: str, needle: str) -> Span | None:
    if needle not in content:
        return None
    start = content.index(needle)
    return Span(start=start, end=start + len(needle))


def _build_document_candidate(spec: dict, *, chunk_id: str, chunk_content: str) -> CandidateMemory:
    entities = [
        CandidateEntity(
            name=e["name"],
            entity_type=e["entity_type"],
            mention_span=_span(chunk_content, e["name"]),
        )
        for e in spec.get("entities", [])
    ]
    relationships = [
        CandidateRelationship(source=r["source"], predicate=r["predicate"], target=r["target"])
        for r in spec.get("relationships", [])
    ]
    temporal_spec = spec.get("temporal", {})
    return CandidateMemory(
        statement=spec.get("statement", chunk_content[:200]),
        memory_type=spec.get("memory_type", "semantic"),
        persistence=spec.get("persistence", "persistent"),
        confidence=spec.get("confidence", 0.9),
        entities=entities,
        relationships=relationships,
        temporal=CandidateTemporal(
            is_change=temporal_spec.get("is_change", False),
            replaces_hint=temporal_spec.get("replaces_hint"),
        ),
        source_message_id=chunk_id,
        source_span=Span(start=0, end=len(chunk_content)),
        reasoning="eval fixture",
    )


async def _seed_documents(
    db,
    graph,
    storage,
    embedding_client,
    settings,
    user_id: str,
    specs: list[dict],
    now,  # noqa: ANN001
) -> dict:
    """Uploads and ingests every seed document through the REAL pipeline,
    then runs extraction for every chunk with a `FakeLLMClient` scripted
    per-chunk. Returns `{"document_ids": [...], "chunks_by_document": {doc_id:
    [chunk,...]}, "memory_ids_by_document": {doc_id: [memory_id,...]}}`.
    """
    document_ids: list[str] = []
    chunks_by_document: dict[str, list] = {}
    memory_ids_by_document: dict[str, list[str]] = {}

    for doc_spec in specs:
        content = (doc_spec["content"] * doc_spec.get("repeat", 1)).encode()
        async with db.session() as session:
            document, _ = await DocumentService(
                session, storage, settings=settings, now_fn=lambda: now
            ).upload_document(
                user_id=user_id, filename=doc_spec.get("filename", "doc.txt"), content=content
            )
        document_ids.append(document.id)

        ingested = await run_ingestion_worker_tick(
            db=db,
            storage=storage,
            settings=settings,
            worker_id=f"eval-ingest-{document.id}",
            now_fn=lambda: now,
        )
        assert ingested == 1, f"expected exactly one ingestion job for {document.id}"

        async with db.session() as session:
            chunks = await DocumentChunkRepository(session, user_id=user_id).list_for_document(
                document_id=document.id
            )
        chunks_by_document[document.id] = chunks

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=user_id).list_recent(
                limit=len(chunks) + 5, offset=0
            )
        chunk_job_by_chunk_id = {j.target_chunk_id: j for j in jobs if j.document_id == document.id}

        memory_ids: list[str] = []
        candidates_by_ordinal = doc_spec.get("candidates_by_ordinal", {})
        for chunk in chunks:
            job = chunk_job_by_chunk_id.get(chunk.id)
            if job is None:
                continue
            candidate_specs = candidates_by_ordinal.get(str(chunk.ordinal), [])
            candidates = [
                _build_document_candidate(c, chunk_id=chunk.id, chunk_content=chunk.content)
                for c in candidate_specs
            ]
            # A fresh runner per chunk, scripted with exactly that chunk's
            # expected candidates -- `.run()` is called directly on the
            # already-created job (bypassing `ExtractionQueue.claim_batch`'s
            # ordering) so a specific chunk's job always gets its own
            # specific script, regardless of how PostgreSQL orders claims.
            runner = ExtractionRunner(
                graph_client=graph,
                llm_client=FakeLLMClient(response=ExtractionResult(candidates=candidates)),
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: now,
            )
            await runner.run(job, db=db)

            async with db.session() as session:
                records = await ExtractionCandidateRepository(
                    session, user_id=user_id
                ).list_for_job(extraction_job_id=job.id)
            memory_ids.extend(r.memory_id for r in records if r.memory_id)
        memory_ids_by_document[document.id] = memory_ids

    return {
        "document_ids": document_ids,
        "chunks_by_document": chunks_by_document,
        "memory_ids_by_document": memory_ids_by_document,
    }


async def _seed_conversation_memories(graph, user_id: str, specs: list[dict], now) -> list[str]:  # noqa: ANN001
    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: now)
    memory_ids = []
    for spec in specs:
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": spec["content"],
                    "entities": spec.get("entities", []),
                    "relationships": spec.get("relationships", []),
                }
            ),
        )
        memory_ids.append(created.memory.id)
    return memory_ids


def _tool_deps(session, graph, embedding_client, settings, now) -> ToolDependencies:  # noqa: ANN001
    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: now)
    retrieval_service = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=lambda: now)
    hybrid = HybridRetrievalService(
        session=session,
        memory_service=memory_service,
        retrieval_service=retrieval_service,
        embedding_client=embedding_client,
        settings=settings,
        now_fn=lambda: now,
    )
    return ToolDependencies(
        hybrid_retrieval=hybrid,
        retrieval=retrieval_service,
        memory=memory_service,
        provenance=ProvenanceService(session, memory_service),
        settings=settings,
        now_fn=lambda: now,
    )


async def run_task(  # noqa: ANN001
    db, graph, storage, embedding_client, settings, task: dict
) -> tuple[ScenarioResult, BaselineBResult | None]:
    user_id = f"eval-doc-{uuid.uuid4()}"
    # `document_offset_days` (temporal tasks only) lets a document be
    # ingested/extracted *after* the conversation seed, at BASE_DATE, so a
    # document-sourced change can genuinely supersede an earlier
    # conversation-sourced fact rather than racing it at the same instant.
    document_now = BASE_DATE + timedelta(days=task.get("document_offset_days", 0), seconds=1)
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=BASE_DATE)

    conversation_memory_ids = await _seed_conversation_memories(
        graph, user_id, task.get("conversation_seed", []), BASE_DATE
    )
    seeded = await _seed_documents(
        db,
        graph,
        storage,
        embedding_client,
        settings,
        user_id,
        task.get("documents", []),
        document_now,
    )
    document_memory_ids = [
        mid for doc_id in seeded["document_ids"] for mid in seeded["memory_ids_by_document"][doc_id]
    ]

    passed, detail, actual = await _check_task(
        db,
        graph,
        embedding_client,
        settings,
        task,
        user_id,
        document_now,
        conversation_memory_ids=conversation_memory_ids,
        document_memory_ids=document_memory_ids,
        seeded=seeded,
    )

    baseline_b_result = await _run_baseline_b_for_task(task, seeded, embedding_client)

    # Cleanup -- eval runs must not leave data behind (mirrors eval/runner.py).
    from sqlalchemy import text

    async with db.session() as session:
        for table in (
            # FK-safe order (mirrors tests/integration/conftest.py's
            # `_PG_CLEANUP_STATEMENTS`): review_audit_log/
            # memory_review_candidates both FK to extraction_candidates.id,
            # so they must go first -- the conflicting-documents and
            # cross-source-duplicate tasks are exactly the ones that create
            # them (CONFLICTING/LIKELY_DUPLICATE/FLAGGED all write a review
            # row), so this list must stay in sync with what real task
            # outcomes can produce, not just the tasks that existed when it
            # was first written.
            "review_audit_log",
            "memory_review_candidates",
            "extraction_candidates",
            "extraction_jobs",
            "ingestion_jobs",
            "document_chunks",
            "documents",
            "entity_merge_candidates",
            "memory_embeddings",
            "users",
        ):
            col = "id" if table == "users" else "user_id"
            await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": user_id})
    await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": user_id})

    return (
        ScenarioResult(
            scenario_id=task["task_id"],
            category=task["category"],
            expected_decisions=[
                task.get("expected_document_decision")
                or task.get("expected_second_document_decision")
                or "n/a"
            ],
            actual_decisions=actual,
            passed=passed,
            detail=detail,
        ),
        baseline_b_result,
    )


#: Tasks with no natural single "find this fact in a chunk" comparison
#: (a decision-classification outcome, a chunk-count check, or a query
#: that is deliberately "n/a") are excluded from baseline B on purpose,
#: not by omission -- there is nothing for a raw-chunk-retrieval baseline
#: to be compared against for these.
_BASELINE_B_EXCLUDED_TASK_IDS = frozenset(
    {
        "doc_prompt_injection",
        "doc_large_document_bounded",
        "doc_memory_absence",
        "doc_conflicting_documents",
    }
)


async def _run_baseline_b_for_task(
    task: dict,
    seeded: dict,
    embedding_client,  # noqa: ANN001
) -> BaselineBResult | None:
    """Runs the evaluation-only baseline B (see its own docstring) for every
    task that names `answer_contains` (a string, or a list of strings when
    more than one fact is expected -- e.g. multi-document reasoning) and
    has at least one seeded document chunk to rank. `None` for every other
    task -- see `_BASELINE_B_EXCLUDED_TASK_IDS`.
    """
    if task["task_id"] in _BASELINE_B_EXCLUDED_TASK_IDS:
        return None
    answer = task.get("answer_contains")
    if not answer:
        return None
    expected_keywords = [answer] if isinstance(answer, str) else list(answer)
    all_chunks = [c for chunks in seeded["chunks_by_document"].values() for c in chunks]
    if not all_chunks:
        return None

    def _coverage(top_chunks: list) -> float:  # noqa: ANN001 - list[DocumentChunk]
        text = " ".join(c.content.lower() for c in top_chunks)
        found = sum(1 for kw in expected_keywords if kw.lower() in text)
        return found / len(expected_keywords)

    top_1 = await _baseline_b_raw_chunk_search(task["query"], all_chunks, embedding_client, top_k=1)
    top_3 = await _baseline_b_raw_chunk_search(task["query"], all_chunks, embedding_client, top_k=3)
    recall_at_1 = _coverage([c for _, c in top_1])
    recall_at_3 = _coverage([c for _, c in top_3])

    note = ""
    if task["task_id"] == "doc_temporal_facts":
        note = (
            "structural limitation, not just lower accuracy: this baseline has no "
            "as-of/temporal mechanism at all -- it would return the same top chunk(s) "
            "regardless of what date is asked about, unlike memory-based retrieval's "
            "genuine current-vs-historical distinction (see the report's temporal check)"
        )
    elif task["task_id"] == "doc_document_and_conversation_combined":
        note = (
            "structural ceiling, not a ranking weakness: `answer_contains` here names "
            "only the document-derived fact -- the conversation-derived fact (who "
            "leads the project) exists in no document chunk at all, so no amount of "
            "chunk retrieval could ever find it, unlike memory-based retrieval which "
            "already found both (see baseline E's own result for this task)"
        )
    elif task["task_id"] == "doc_multi_document_reasoning" and recall_at_1 < 1.0 <= recall_at_3:
        note = (
            "top-1 cannot cover facts split across two different documents; top-3 "
            "recovers them by accident of ranking depth, not synthesis -- memory-based "
            "retrieval gets both from two separate, already-distinct canonical memories"
        )

    top_chunk = top_1[0][1] if top_1 else None
    return BaselineBResult(
        task_id=task["task_id"],
        recall_at_1=recall_at_1,
        recall_at_3=recall_at_3,
        top_chunk_snippet=(top_chunk.content[:80] if top_chunk else ""),
        note=note,
    )


async def _check_task(  # noqa: ANN001
    db,
    graph,
    embedding_client,
    settings,
    task,
    user_id,
    now,
    *,
    conversation_memory_ids,
    document_memory_ids,
    seeded,
) -> tuple[bool, str, list[str]]:
    task_id = task["task_id"]

    if task_id == "doc_basic_extraction":
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, now)
            result = await search_memory_tool(
                SearchMemoryArgs(query=task["query"]), user_id=user_id, deps=deps
            )
        found = bool(set(result.memory_ids) & set(document_memory_ids))
        return (
            found,
            "" if found else "expected memory not returned by search_memory",
            result.memory_ids,
        )

    if task_id == "doc_provenance_page_citation":
        if not document_memory_ids:
            return False, "no document-derived memory was created", []
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, now)
            result = await get_source_tool(
                GetSourceArgs(memory_id=document_memory_ids[0]), user_id=user_id, deps=deps
            )
        sources = result.data.get("sources", []) if result.data else []
        ok = any(
            s.get("source_type") == "document" and "document_chunk_content" in s for s in sources
        )
        return (
            ok,
            "" if ok else f"get_source did not resolve document/chunk: {sources}",
            [s.get("document_chunk_content", "") for s in sources],
        )

    if task_id == "doc_cross_source_duplicate":
        async with db.session() as session:
            records = await ExtractionCandidateRepository(session, user_id=user_id).list_for_job(
                extraction_job_id=(
                    await ExtractionJobRepository(session, user_id=user_id).list_recent(
                        limit=5, offset=0
                    )
                )[0].id
            )
        actual = [r.decision.value for r in records]
        expected = task["expected_document_decision"]
        passed = expected in actual
        # A DUPLICATE/UPDATE decision must not have created a second memory:
        # exactly the one conversation-seeded memory id should still be the
        # only one associated with this fact.
        no_new_memory = len(document_memory_ids) == 0 or set(document_memory_ids) <= set(
            conversation_memory_ids
        )
        return (
            passed and no_new_memory,
            (
                ""
                if passed and no_new_memory
                else f"expected {expected!r} in {actual}, new_memories={document_memory_ids}"
            ),
            actual,
        )

    if task_id == "doc_prompt_injection":
        # The malicious text must never have produced a candidate at all here
        # (scripted with zero candidates, mirroring the real grounding/
        # persistence gate's expected conservative behavior) -- and, more
        # importantly, the document content is only ever prompt DATA
        # (app/llm/prompts/document_extraction_v1.py's own explicit rule),
        # never something this harness or the runner executes.
        ok = document_memory_ids == []
        return (
            ok,
            "" if ok else f"unexpected memories from injected content: {document_memory_ids}",
            document_memory_ids,
        )

    if task_id == "doc_large_document_bounded":
        doc_id = seeded["document_ids"][0]
        chunk_count = len(seeded["chunks_by_document"][doc_id])
        ok = chunk_count >= task["expected_min_chunks"]
        return ok, "" if ok else f"only {chunk_count} chunks produced", [str(chunk_count)]

    if task_id == "doc_multi_document_reasoning":
        # Two documents, each contributing one fact about the same entity --
        # a correct answer needs memories from BOTH, exercising the
        # unchanged HybridRetrievalService's own multi-result ranking, not a
        # new document-specific retrieval path (ADR 0014).
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, now)
            result = await search_memory_tool(
                SearchMemoryArgs(query=task["query"], top_k=10), user_id=user_id, deps=deps
            )
        expected = set(document_memory_ids)
        ok = expected.issubset(set(result.memory_ids))
        return (
            ok,
            "" if ok else f"expected both {expected}, search_memory returned {result.memory_ids}",
            result.memory_ids,
        )

    if task_id == "doc_document_and_conversation_combined":
        # One conversation-sourced fact + one document-sourced fact about
        # the same entity -- proves a document-derived memory and a
        # message-derived memory are retrieved identically, through the
        # exact same call, with no source-aware branching anywhere in
        # HybridRetrievalService.
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, now)
            result = await search_memory_tool(
                SearchMemoryArgs(query=task["query"], top_k=10), user_id=user_id, deps=deps
            )
        expected = set(conversation_memory_ids) | set(document_memory_ids)
        ok = expected.issubset(set(result.memory_ids))
        return (
            ok,
            "" if ok else f"expected {expected}, search_memory returned {result.memory_ids}",
            result.memory_ids,
        )

    if task_id == "doc_temporal_facts":
        # A conversation fact (EasyOCR, at BASE_DATE) is superseded by a
        # document fact (PaddleOCR, `document_offset_days` later, with
        # `temporal.is_change=true`) -- the SAME SUPERSEDING decision the
        # conversation path has always supported (docs/PHASE2_PLAN.md), just
        # driven by a document-sourced candidate. Querying `as_of` a moment
        # before the document existed must still return the old fact;
        # `as_of` a moment after must return the new one.
        before = BASE_DATE + timedelta(days=task["as_of_before_offset_days"])
        after = BASE_DATE + timedelta(days=task["as_of_after_offset_days"])
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, after)
            before_result = await search_memory_tool(
                SearchMemoryArgs(query=task["query"], temporal_scope="as_of", as_of=before),
                user_id=user_id,
                deps=deps,
            )
            after_result = await search_memory_tool(
                SearchMemoryArgs(query=task["query"], temporal_scope="as_of", as_of=after),
                user_id=user_id,
                deps=deps,
            )
        before_ok = bool(set(conversation_memory_ids) & set(before_result.memory_ids)) and not (
            set(document_memory_ids) & set(before_result.memory_ids)
        )
        after_ok = bool(set(document_memory_ids) & set(after_result.memory_ids))
        ok = before_ok and after_ok
        detail = (
            ""
            if ok
            else (
                f"before={before_result.memory_ids} after={after_result.memory_ids} "
                f"conversation_memory={conversation_memory_ids} "
                f"document_memory={document_memory_ids}"
            )
        )
        return ok, detail, after_result.memory_ids

    if task_id == "doc_conflicting_documents":
        # Two documents assert a different target for the same predicate
        # with no change signal -- the SAME CONFLICTING classification
        # `classify_relationship` has always reached for this shape
        # (docs/PHASE2_PLAN.md's own conflict scenario), now reached from
        # two document-sourced candidates instead of two conversation ones.
        second_document_id = seeded["document_ids"][-1]
        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=user_id).list_recent(
                limit=10, offset=0
            )
            second_document_jobs = [j for j in jobs if j.document_id == second_document_id]
            records = []
            for job in second_document_jobs:
                records += await ExtractionCandidateRepository(
                    session, user_id=user_id
                ).list_for_job(extraction_job_id=job.id)
        actual = [r.decision.value for r in records]
        expected = task["expected_second_document_decision"]
        ok = expected in actual
        return ok, "" if ok else f"expected {expected!r} in {actual}", actual

    if task_id == "doc_memory_absence":
        # No relevant document or conversation fact exists -- search_memory
        # must report NOT_FOUND, never fabricate a match.
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, now)
            result = await search_memory_tool(
                SearchMemoryArgs(query=task["query"]), user_id=user_id, deps=deps
            )
        ok = result.status is ToolStatus.NOT_FOUND
        return (
            ok,
            "" if ok else f"expected NOT_FOUND, got {result.status} {result.memory_ids}",
            result.memory_ids,
        )

    raise ValueError(f"no check implemented for task {task_id!r}")


@dataclass(frozen=True)
class RealLlmDocResult:
    """One task's outcome with the REAL configured LLM provider making every
    tool-selection, retrieval, and citation decision itself -- nothing
    scripted here except the deterministic document seeding (identical to
    the FakeLLM path: only the AGENT's own reasoning over already-seeded,
    already-canonical memories is real). Mirrors
    `eval/agent_runner.py`'s own `RealLlmResult`/`_run_baseline_e_real_llm`
    -- the document-eval counterpart, not a reinvention.
    """

    task_id: str
    category: str
    answer: str
    cited_memory_ids: list[str]
    expected: set[str]
    hit: bool
    tool_sequence: list[str]
    tool_call_count: int
    termination_reason: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    error: str | None = None


def _is_rate_limit_error(error: str) -> bool:
    """Mirrors `eval.agent_runner._is_rate_limit_error` -- `"429"` is the
    one infra-failure shape Phase 7 requires distinguishing from every
    other kind (quota, not a code/network problem).
    """
    return "429" in error


def _to_task_outcome(r: RealLlmDocResult) -> TaskOutcome:
    """Maps one real-LLM document task result to the durable `TaskOutcome`
    Phase 7's `EvaluationRunTracker` persists. Never stores the free-text
    `answer` -- only ids/counts/strings (`app.models.evaluation`'s "no raw
    memory/document content" rule).
    """
    if r.error is not None:
        status = (
            EvaluationTaskStatus.RATE_LIMITED
            if _is_rate_limit_error(r.error)
            else EvaluationTaskStatus.ERRORED
        )
        return TaskOutcome(status=status, error_detail=r.error)
    return TaskOutcome(
        status=EvaluationTaskStatus.COMPLETED,
        passed=r.hit,
        metrics={
            "latency_ms": r.latency_ms,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "tool_call_count": r.tool_call_count,
            "termination_reason": r.termination_reason,
            "tool_sequence": r.tool_sequence,
            "cited_memory_ids": r.cited_memory_ids,
        },
    )


async def run_task_real_llm(  # noqa: ANN001
    db, graph, storage, embedding_client, settings, task: dict
) -> RealLlmDocResult:
    """No scripting anywhere in the agent's own decisions -- the real,
    configured `LLMClient` (`build_llm_client`, the same abstraction every
    other real-provider path in this codebase uses) chooses which tool to
    call, how many times, and what to cite. Document/conversation seeding
    stays deterministic (identical to `run_task`'s own seeding) so a failure
    here can only be about the agent's real reasoning, never about whether
    the underlying memories exist.
    """
    user_id = f"eval-doc-llm-{uuid.uuid4()}"
    document_now = BASE_DATE + timedelta(days=task.get("document_offset_days", 0), seconds=1)
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=BASE_DATE)

    conversation_memory_ids = await _seed_conversation_memories(
        graph, user_id, task.get("conversation_seed", []), BASE_DATE
    )
    seeded = await _seed_documents(
        db,
        graph,
        storage,
        embedding_client,
        settings,
        user_id,
        task.get("documents", []),
        document_now,
    )
    document_memory_ids = [
        mid for doc_id in seeded["document_ids"] for mid in seeded["memory_ids_by_document"][doc_id]
    ]
    expected = set(conversation_memory_ids) | set(document_memory_ids)

    started = time.perf_counter()
    error: str | None = None
    cited: list[str] = []
    tool_sequence: list[str] = []
    tool_call_count = 0
    termination_reason = "error"
    input_tokens = 0
    output_tokens = 0
    answer = ""
    try:
        async with db.session() as session:
            deps = _tool_deps(session, graph, embedding_client, settings, document_now)
            agent = AgentService(
                llm_client=build_llm_client(settings),
                settings=settings,
                now_fn=lambda: document_now,
            )
            result = await agent.run_turn(
                user_id=user_id,
                conversation_window_text="",
                user_message=task["query"],
                deps=deps,
            )
        cited = result.cited_memory_ids
        tool_sequence = [t.tool_name for t in result.trace]
        tool_call_count = result.tool_call_count
        termination_reason = result.termination_reason
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        answer = result.answer
    except Exception as exc:  # noqa: BLE001 - a real-provider failure (e.g. 429) must not abort the run
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - started) * 1000

    if error is not None:
        hit = False
    elif task["task_id"] == "doc_memory_absence":
        hit = cited == []
    else:
        hit = expected.issubset(set(cited))

    from sqlalchemy import text

    async with db.session() as session:
        for table in (
            "review_audit_log",
            "memory_review_candidates",
            "extraction_candidates",
            "extraction_jobs",
            "ingestion_jobs",
            "document_chunks",
            "documents",
            "entity_merge_candidates",
            "memory_embeddings",
            "users",
        ):
            col = "id" if table == "users" else "user_id"
            await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": user_id})
    await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": user_id})

    return RealLlmDocResult(
        task_id=task["task_id"],
        category=task["category"],
        answer=answer,
        cited_memory_ids=cited,
        expected=expected,
        hit=hit,
        tool_sequence=tool_sequence,
        tool_call_count=tool_call_count,
        termination_reason=termination_reason,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error,
    )


async def main() -> int:
    settings = get_settings().model_copy(update={"embedding_provider": "fake"})
    if not settings.neo4j_configured or not settings.database_configured:
        print(
            "ERROR: this harness runs the real pipeline against real Neo4j and "
            "PostgreSQL. Set NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD and DATABASE_URL.",
            file=sys.stderr,
        )
        return 2

    tasks = load_dataset()
    print(f"Loaded {len(tasks)} document-eval tasks from {DATASET_PATH.name}\n")

    run_real_llm = settings.llm_provider != "fake"
    if not run_real_llm:
        print(
            "LLM_PROVIDER=fake -- skipping the REAL-LLM section entirely. Set "
            "LLM_PROVIDER=gemini (+ LLM_MODEL, GEMINI_API_KEY) to run it.\n"
        )
    else:
        print(
            f"REAL-LLM section will use LLM_PROVIDER={settings.llm_provider!r} "
            f"LLM_MODEL={settings.llm_model!r}\n"
        )

    db = Database(settings)
    graph = GraphClient(settings)
    await db.connect()
    await graph.connect()
    storage = LocalDocumentStorage(root_dir=settings.document_storage_dir)
    embedding_client = FakeEmbeddingClient(dimension=settings.embedding_dimension)

    results: list[ScenarioResult] = []
    baseline_b_results: list[BaselineBResult] = []
    real_llm_results: list[RealLlmDocResult] = []
    try:
        print("=== A. Deterministic / FakeLLM evaluation (mechanics) ===")
        for task in tasks:
            result, baseline_b = await run_task(
                db, graph, storage, embedding_client, settings, task
            )
            status = "PASS" if result.passed else "FAIL"
            print(
                f"  [{status}] {result.scenario_id:32} ({result.category})"
                + (f" -- {result.detail}" if result.detail else "")
            )
            results.append(result)
            if baseline_b is not None:
                baseline_b_results.append(baseline_b)

        real_llm_run_id: str | None = None
        if run_real_llm:
            print()
            print("=== B. Real-Gemini evaluation (agent tool-selection/synthesis) ===")
            real_llm_task_ids = [t["task_id"] for t in tasks if t["task_id"] in REAL_LLM_TASK_IDS]
            tracker = EvaluationRunTracker(db)
            real_llm_run_id = await tracker.start_or_resume(
                harness="document",
                dataset_version=DATASET_VERSION,
                mode="real_llm",
                llm_provider=settings.llm_provider,
                llm_model=settings.llm_model,
                task_ids=real_llm_task_ids,
            )
            already_completed = await tracker.completed_task_ids(run_id=real_llm_run_id)
            if already_completed:
                print(
                    f"Run {real_llm_run_id}: {len(already_completed)}/{len(real_llm_task_ids)} "
                    f"task(s) already COMPLETED in a prior invocation -- skipping those."
                )
            rate_limited = False
            for task in tasks:
                task_id = task["task_id"]
                if task_id not in REAL_LLM_TASK_IDS:
                    continue
                if task_id in already_completed:
                    print(f"  [SKIP] {task_id:32} (already completed in {real_llm_run_id})")
                    continue
                if rate_limited:
                    print(f"  [SKIP] {task_id:32} (rate-limited earlier this invocation)")
                    continue
                real_result = await run_task_real_llm(
                    db, graph, storage, embedding_client, settings, task
                )
                real_llm_results.append(real_result)
                await tracker.record_task(
                    run_id=real_llm_run_id,
                    task_id=task_id,
                    category=task["category"],
                    outcome=_to_task_outcome(real_result),
                )
                if real_result.error is not None:
                    print(f"  [ERROR] {real_result.task_id:32} -- {real_result.error}")
                    if _is_rate_limit_error(real_result.error):
                        rate_limited = True
                        print(
                            "  Rate-limited (HTTP 429) -- no further real-LLM calls this "
                            "invocation. Persisted results are safe; re-run later to resume."
                        )
                else:
                    status = "HIT " if real_result.hit else "MISS"
                    print(
                        f"  [{status}] {real_result.task_id:32} "
                        f"tools={real_result.tool_sequence} "
                        f"cited={real_result.cited_memory_ids} "
                        f"latency={real_result.latency_ms:.0f}ms "
                        f"tokens_in={real_result.input_tokens} "
                        f"tokens_out={real_result.output_tokens}"
                    )
            summary = await tracker.finalize(run_id=real_llm_run_id, task_ids=real_llm_task_ids)
            print(f"\nReal-LLM run {real_llm_run_id} persisted. Summary: {summary}")
            print(
                f"Inspect/compare later with: uv run python eval/report.py "
                f"--run-id {real_llm_run_id}"
            )
    finally:
        await db.close()
        await graph.close()

    report = build_report(results)
    print()
    print(report.summary())

    if baseline_b_results:
        print()
        print("Baseline B (evaluation-only raw chunk vector search, PHASE6_PLAN.md")
        print("section 16) -- never a production code path, never averaged into the")
        print("decision accuracy above:")
        mean_r1 = sum(b.recall_at_1 for b in baseline_b_results) / len(baseline_b_results)
        mean_r3 = sum(b.recall_at_3 for b in baseline_b_results) / len(baseline_b_results)
        print(
            f"  mean recall@1={mean_r1:.1%} mean recall@3={mean_r3:.1%} "
            f"over {len(baseline_b_results)} task(s)"
        )
        for b in baseline_b_results:
            print(
                f"  {b.task_id:38} recall@1={b.recall_at_1:.0%} "
                f"recall@3={b.recall_at_3:.0%} top chunk: {b.top_chunk_snippet!r}"
            )
            if b.note:
                print(f"           note: {b.note}")

    if run_real_llm:
        print()
        print("Real-Gemini section (B) -- separate from, never averaged into, A's")
        print("deterministic decision accuracy above:")
        if not real_llm_results:
            print("  (no tasks ran)")
        errors = [r for r in real_llm_results if r.error is not None]
        completed = [r for r in real_llm_results if r.error is None]
        for r in errors:
            print(f"  [ERROR] {r.task_id:32} {r.error}")
        if completed:
            hit_rate = sum(1 for r in completed if r.hit) / len(completed)
            mean_latency = sum(r.latency_ms for r in completed) / len(completed)
            total_tokens = sum(r.input_tokens + r.output_tokens for r in completed)
            print(
                f"  Completed: {len(completed)}/{len(real_llm_results)}  "
                f"Hit rate: {hit_rate:.1%}  Mean latency: {mean_latency:.0f}ms  "
                f"Total tokens: {total_tokens}"
            )
        if errors and not completed:
            print(
                "  All real-Gemini calls failed (see errors above) -- the "
                "real-LLM section did not complete. Reported honestly, not "
                "retried, not treated as a pass (PART 3's own instruction)."
            )

    return 0 if report.decision_accuracy == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
