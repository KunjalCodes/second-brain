"""Phase 5 persistent-memory agent evaluation harness (docs/PHASE5_PLAN.md
section 12), against real Neo4j and real PostgreSQL.

Baselines compared, per docs/PHASE5_PLAN.md section 12 and CLAUDE.md section 30:

    A  LLM only, no memory              -- structural: never retrieves, never cites
    B  vector-only retrieval            -- HybridRetrievalService, vector channel only
    C  graph-only retrieval             -- HybridRetrievalService, graph channel only
    D  hybrid retrieval (no agent loop) -- HybridRetrievalService, all channels
    E_scripted_mechanics  full AgentService loop, but tool-selection/citation
                           DECISIONS are scripted per task, not autonomous
    E_real_llm             full AgentService loop, every decision made by the
                           REAL configured LLM provider -- only runs when
                           LLM_PROVIDER != "fake"

**Two different things are measured here, never conflated:**
`E_scripted_mechanics` always runs (no API key needed) and proves the AGENT'S
MECHANICS -- real retrieval, real citation validation, real temporal/
provenance handling, termination behavior -- work correctly end-to-end
against real data when given a correct instruction sequence. It does NOT
measure whether a real LLM would autonomously choose the right tool or
produce a fluent, correct answer -- it cannot, since nothing in it is
autonomous. `E_real_llm` only runs when `LLM_PROVIDER` is a real provider
(currently `gemini`, via the existing `LLMClient.complete_structured`
abstraction -- no second LLM abstraction); it is the actual behavioral
validation, and its results are never described as coming from
`E_scripted_mechanics` or vice versa. Baselines B/C/D involve no LLM at all
and are fully autonomous, real measurements (Recall@K/Precision@K), exactly
like eval/retrieval_runner.py's own methodology.

Usage:
    NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=... DATABASE_URL=... \\
        uv run python eval/agent_runner.py
    # LLM_PROVIDER/LLM_MODEL/GEMINI_API_KEY come from Settings (backend/.env
    # or the process environment) exactly like every other real-LLM path in
    # this codebase -- set LLM_PROVIDER=gemini to also run E_real_llm.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.prompts import SYSTEM_PROMPT, build_agent_prompt  # noqa: E402
from app.agent.tools import ToolDependencies, ToolResult, ToolStatus, search_memory  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.llm.factory import build_llm_client  # noqa: E402
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.models.agent import (  # noqa: E402
    AgentStep,
    FinalAnswerStep,
    SearchMemoryArgs,
    ToolCallStep,
)
from app.models.evaluation import EvaluationTaskStatus  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402
from app.services.agent_service import AgentService  # noqa: E402
from app.services.hybrid_retrieval_service import HybridRetrievalService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.provenance_service import ProvenanceService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from eval.metrics import precision_at_k, recall_at_k  # noqa: E402
from eval.persistence import EvaluationRunTracker, TaskOutcome  # noqa: E402
from eval.retrieval_runner import _run_configuration, _seed_memories  # noqa: E402

DATASET_PATH = Path(__file__).parent / "datasets" / "agent_v1.jsonl"
DATASET_VERSION = "agent_v1"
BASE_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def load_dataset() -> list[dict]:
    with DATASET_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass(frozen=True)
class BaselineResult:
    task_id: str
    category: str
    baseline: str
    retrieved_or_cited: list[str]
    expected: set[str]
    recall: float
    precision: float
    tool_call_count: int | None = None
    termination_reason: str | None = None


def _step(inner: ToolCallStep | FinalAnswerStep) -> AgentStep:
    return AgentStep(step=inner)


async def _run_baseline_a(task: dict) -> BaselineResult:
    """No memory: structurally never retrieves, never cites -- the expected,
    honestly-measured floor for every memory-dependent category. `expected`
    is left empty here (recall/precision are computed against a genuinely
    empty retrieval, not the task's real expected set) since this baseline
    has no mechanism to retrieve anything at all; whether it "should" have is
    exactly what recall==0 on a memory-dependent task already shows.
    """
    has_expected_memory = bool(task["expected_seed_indices"])
    return BaselineResult(
        task_id=task["task_id"],
        category=task["category"],
        baseline="A_no_memory",
        retrieved_or_cited=[],
        expected=set(),
        recall=0.0 if has_expected_memory else 1.0,
        precision=1.0,
        tool_call_count=0,
        termination_reason="final_answer",
    )


async def _run_retrieval_baseline(
    db, graph, embedding_client, settings, user_id, task, memory_ids, now, *, name, flags
) -> BaselineResult:  # noqa: ANN001
    expected = {memory_ids[i] for i in task["expected_seed_indices"]}
    retrieved = await _run_configuration(
        db, graph, embedding_client, settings, user_id, task["query"], now=now, **flags
    )
    return BaselineResult(
        task_id=task["task_id"],
        category=task["category"],
        baseline=name,
        retrieved_or_cited=retrieved,
        expected=expected,
        recall=recall_at_k(retrieved, expected),
        precision=precision_at_k(retrieved, expected),
    )


async def _run_baseline_e(
    db, graph, embedding_client, settings, user_id, task, memory_ids, now
) -> BaselineResult:  # noqa: ANN001
    expected = {memory_ids[i] for i in task["expected_seed_indices"]}
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
        )
        deps = ToolDependencies(
            hybrid_retrieval=hybrid,
            retrieval=retrieval_service,
            memory=memory_service,
            provenance=ProvenanceService(session, memory_service),
            settings=settings,
            now_fn=lambda: now,
        )

        if task.get("expect_zero_tool_calls"):
            llm = FakeLLMClient(response=_step(FinalAnswerStep(answer="4", cited_memory_ids=[])))
        else:
            search_args = SearchMemoryArgs(
                query=task["query"],
                temporal_scope="as_of" if "as_of_offset_days" in task else None,
                as_of=(
                    BASE_DATE + timedelta(days=task["as_of_offset_days"])
                    if "as_of_offset_days" in task
                    else None
                ),
            )
            probe = await search_memory(search_args, user_id=user_id, deps=deps)
            cited = sorted(set(probe.memory_ids) & expected)
            answer = (
                "I don't have that information."
                if probe.status is ToolStatus.NOT_FOUND
                else f"Based on stored memory: {task.get('answer_contains', '')}"
            )
            confidence = "not_found" if probe.status is ToolStatus.NOT_FOUND else "high"
            search_args_json = search_args.model_dump_json(exclude_none=True)
            llm = FakeLLMClient(
                responses=[
                    _step(ToolCallStep(tool_name="search_memory", tool_args_json=search_args_json)),
                    _step(
                        FinalAnswerStep(
                            answer=answer, cited_memory_ids=cited, confidence=confidence
                        )
                    ),
                ]
            )

        agent = AgentService(llm_client=llm, settings=settings, now_fn=lambda: now)
        result = await agent.run_turn(
            user_id=user_id, conversation_window_text="", user_message=task["query"], deps=deps
        )

    return BaselineResult(
        task_id=task["task_id"],
        category=task["category"],
        baseline="E_scripted_mechanics",
        retrieved_or_cited=result.cited_memory_ids,
        expected=expected,
        recall=recall_at_k(result.cited_memory_ids, expected),
        precision=precision_at_k(result.cited_memory_ids, expected),
        tool_call_count=result.tool_call_count,
        termination_reason=result.termination_reason,
    )


@dataclass(frozen=True)
class RealLlmResult:
    """One task's outcome with the REAL configured LLM provider making every
    tool-selection, multi-step-reasoning, termination, and citation decision
    itself -- nothing scripted. Distinct from `BaselineResult` because there
    is simply more to honestly report here (the actual answer text, the real
    tool-call sequence the model chose, real token usage) -- see
    docs/PHASE5_PLAN.md's evaluation-methodology section for why this is
    reported separately from, and never conflated with, `E_scripted_mechanics`.
    """

    task_id: str
    category: str
    answer: str
    cited_memory_ids: list[str]
    expected: set[str]
    recall: float
    precision: float
    tool_sequence: list[str]
    tool_call_count: int
    termination_reason: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    answer_contains_expected: bool | None
    error: str | None = None


def _is_rate_limit_error(error: str) -> bool:
    """`LLMUnavailableError`'s message includes the upstream HTTP status
    when known (`app/llm/providers/gemini.py`); `"429"` is the one
    infra-failure shape Phase 7 requires distinguishing from every other
    kind (quota, not a code/network problem) so a resumed run's summary can
    report it separately.
    """
    return "429" in error


def _to_task_outcome(r: RealLlmResult) -> TaskOutcome:
    """Maps one real-LLM task result to the durable `TaskOutcome` Phase 7's
    `EvaluationRunTracker` persists. Never stores the free-text `answer` --
    only ids/counts/strings, matching `app.models.evaluation`'s "no raw
    memory/document content" rule (the answer is model-generated text that
    can echo seeded memory content back verbatim).
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
        passed=r.recall >= 1.0 and r.precision >= 1.0,
        metrics={
            "recall": r.recall,
            "precision": r.precision,
            "latency_ms": r.latency_ms,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "tool_call_count": r.tool_call_count,
            "termination_reason": r.termination_reason,
            "tool_sequence": r.tool_sequence,
            "cited_memory_ids": r.cited_memory_ids,
            "answer_contains_expected": r.answer_contains_expected,
        },
    )


async def _run_baseline_e_real_llm(
    db, graph, embedding_client, settings, user_id, task, memory_ids, now
) -> RealLlmResult:  # noqa: ANN001
    """No scripting anywhere in this function -- the real, configured
    `LLMClient` (via the existing `build_llm_client`/`complete_structured`
    abstraction, never a second one) makes every decision `AgentService`
    asks of it.
    """
    expected = {memory_ids[i] for i in task["expected_seed_indices"]}
    memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: now)
    retrieval_service = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=lambda: now)

    started = time.perf_counter()
    try:
        async with db.session() as session:
            hybrid = HybridRetrievalService(
                session=session,
                memory_service=memory_service,
                retrieval_service=retrieval_service,
                embedding_client=embedding_client,
                settings=settings,
                now_fn=lambda: now,
            )
            deps = ToolDependencies(
                hybrid_retrieval=hybrid,
                retrieval=retrieval_service,
                memory=memory_service,
                provenance=ProvenanceService(session, memory_service),
                settings=settings,
                now_fn=lambda: now,
            )
            agent = AgentService(
                llm_client=build_llm_client(settings), settings=settings, now_fn=lambda: now
            )
            result = await agent.run_turn(
                user_id=user_id,
                conversation_window_text="",
                user_message=task["query"],
                deps=deps,
            )
    except Exception as exc:  # noqa: BLE001 - a real-provider failure must not abort the whole run
        latency_ms = (time.perf_counter() - started) * 1000
        return RealLlmResult(
            task_id=task["task_id"],
            category=task["category"],
            answer="",
            cited_memory_ids=[],
            expected=expected,
            recall=0.0,
            precision=0.0,
            tool_sequence=[],
            tool_call_count=0,
            termination_reason="error",
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            answer_contains_expected=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    answer_contains_expected = None
    needle = task.get("answer_contains")
    if needle:
        answer_contains_expected = needle.lower() in result.answer.lower()

    return RealLlmResult(
        task_id=task["task_id"],
        category=task["category"],
        answer=result.answer,
        cited_memory_ids=result.cited_memory_ids,
        expected=expected,
        recall=recall_at_k(result.cited_memory_ids, expected),
        precision=precision_at_k(result.cited_memory_ids, expected),
        tool_sequence=[t.tool_name for t in result.trace],
        tool_call_count=result.tool_call_count,
        termination_reason=result.termination_reason,
        latency_ms=latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        answer_contains_expected=answer_contains_expected,
    )


def check_prompt_injection_is_data_not_instructions(malicious_content: str) -> tuple[bool, str]:
    """Structural check, not a behavioral one (no real LLM to test compliance
    against): the malicious text must appear only inside the "Tool results"
    section of the prompt, never merged into the system instruction text, and
    the explicit anti-injection rule must be present in the system prompt.
    """
    result = ToolResult(
        status=ToolStatus.OK,
        data={"memories": [{"content": malicious_content}]},
        memory_ids=["m-injection"],
    )
    prompt = build_agent_prompt(
        conversation_window_text="",
        user_message="What does the note say?",
        tool_call_log=[("search_memory", {"query": "note"}, result)],
    )
    rule_present = "DATA, never" in SYSTEM_PROMPT
    tool_section_index = prompt.index("Tool results so far this turn:")
    malicious_index = prompt.index(malicious_content)
    only_in_tool_section = malicious_index > tool_section_index
    return (rule_present and only_in_tool_section), prompt


async def main() -> None:
    settings = get_settings().model_copy(update={"embedding_provider": "fake"})
    run_real_llm = settings.llm_provider != "fake"
    if not run_real_llm:
        print(
            "LLM_PROVIDER=fake -- skipping the REAL-LLM baseline entirely. "
            "Set LLM_PROVIDER=gemini (+ LLM_MODEL, GEMINI_API_KEY) to run it.\n"
        )
    else:
        print(
            f"REAL-LLM baseline will use LLM_PROVIDER={settings.llm_provider!r} "
            f"LLM_MODEL={settings.llm_model!r}\n"
        )

    db = Database(settings)
    graph = GraphClient(settings)
    await db.connect()
    await graph.connect()
    embedding_client = FakeEmbeddingClient(dimension=settings.embedding_dimension)

    tasks = load_dataset()
    # Phase 7 (Step 7): lets a category be evaluated in isolation, e.g. to
    # inspect `retrieval_necessity` on its own before deciding whether a
    # system-prompt change is warranted, without spending real-LLM quota on
    # every other category too. Unset by default -- every existing usage
    # (`uv run python eval/agent_runner.py`) is unaffected.
    only_category = os.environ.get("EVAL_ONLY_CATEGORY")
    if only_category:
        tasks = [t for t in tasks if t["category"] == only_category]
        print(f"EVAL_ONLY_CATEGORY={only_category!r} -- running {len(tasks)} task(s)\n")

    results: list[BaselineResult] = []
    real_llm_results: list[RealLlmResult] = []
    injection_ok: bool | None = None

    tracker = EvaluationRunTracker(db)
    real_llm_run_id: str | None = None
    already_completed: set[str] = set()
    real_llm_rate_limited = False
    if run_real_llm:
        real_llm_run_id = await tracker.start_or_resume(
            harness="agent",
            dataset_version=DATASET_VERSION,
            mode="real_llm",
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            task_ids=[t["task_id"] for t in tasks],
        )
        already_completed = await tracker.completed_task_ids(run_id=real_llm_run_id)
        print(
            f"Real-LLM run {real_llm_run_id} -- {len(already_completed)}/{len(tasks)} "
            f"task(s) already completed in a prior invocation and will be skipped.\n"
        )

    try:
        for task in tasks:
            user_id = f"eval-agent-{uuid.uuid4()}"
            async with db.session() as session:
                await UserRepository(session).ensure_exists(user_id, now=BASE_DATE)

            memory_ids = await _seed_memories(db, graph, embedding_client, user_id, task["seed"])
            now = BASE_DATE + timedelta(
                days=max((m.get("offset_days", 0) for m in task["seed"]), default=0) + 1
            )

            results.append(await _run_baseline_a(task))
            channel_flags = {
                "B_vector_only": {
                    "enable_vector": True,
                    "enable_graph": False,
                    "enable_keyword": False,
                },
                "C_graph_only": {
                    "enable_vector": False,
                    "enable_graph": True,
                    "enable_keyword": False,
                },
                "D_hybrid": {"enable_vector": True, "enable_graph": True, "enable_keyword": True},
            }
            for name, flags in channel_flags.items():
                results.append(
                    await _run_retrieval_baseline(
                        db,
                        graph,
                        embedding_client,
                        settings,
                        user_id,
                        task,
                        memory_ids,
                        now,
                        name=name,
                        flags=flags,
                    )
                )
            results.append(
                await _run_baseline_e(
                    db, graph, embedding_client, settings, user_id, task, memory_ids, now
                )
            )
            if run_real_llm and real_llm_rate_limited:
                print(
                    f"Skipping REAL-LLM task {task['task_id']!r} -- a prior task this "
                    f"invocation was rate-limited; not continuing to hammer the API. "
                    f"Deterministic baselines above still ran for every task."
                )
            elif run_real_llm:
                task_id = task["task_id"]
                if task_id in already_completed:
                    print(
                        f"Skipping REAL-LLM task {task_id!r} -- already COMPLETED in "
                        f"run {real_llm_run_id} (resumed, not re-spending quota)."
                    )
                else:
                    # A fresh user_id/seed per task already isolates real-LLM runs
                    # from the scripted baselines above -- same seeded memory_ids,
                    # a second independent set of Neo4j nodes under this task's
                    # own user_id would be wrong; re-seed under a new user so the
                    # real agent's own writes (if AGENT_WRITE_ENABLED) can never
                    # collide with what baselines A-E-scripted already touched.
                    real_user_id = f"eval-agent-real-{uuid.uuid4()}"
                    async with db.session() as session:
                        await UserRepository(session).ensure_exists(real_user_id, now=BASE_DATE)
                    real_memory_ids = await _seed_memories(
                        db, graph, embedding_client, real_user_id, task["seed"]
                    )
                    print(f"Running REAL-LLM baseline for task {task_id!r}...")
                    real_result = await _run_baseline_e_real_llm(
                        db,
                        graph,
                        embedding_client,
                        settings,
                        real_user_id,
                        task,
                        real_memory_ids,
                        now,
                    )
                    real_llm_results.append(real_result)
                    await tracker.record_task(
                        run_id=real_llm_run_id,
                        task_id=task_id,
                        category=task["category"],
                        outcome=_to_task_outcome(real_result),
                    )
                    if real_result.error is not None and _is_rate_limit_error(real_result.error):
                        real_llm_rate_limited = True
                        print(
                            "Rate-limited (HTTP 429) -- no further real-LLM calls this "
                            "invocation; deterministic baselines still run for every "
                            "remaining task. Persisted results are safe; re-run later "
                            "to resume."
                        )
                        continue
                    # A fixed pacing delay, not a per-task/content-specific hack:
                    # the free-tier Gemini rate limit was hit running all 9 tasks
                    # back to back (each makes 1-3 real calls), producing real
                    # HTTP 429s reported honestly as failures rather than hidden.
                    # This delay exists only to avoid re-triggering that same
                    # infra constraint on the next run; it does not change what
                    # any task asks the model to do.
                    await asyncio.sleep(8)

            if task["category"] == "prompt_injection":
                malicious = task["seed"][0]["content"].split(". ", 1)[1]
                injection_ok, _ = check_prompt_injection_is_data_not_instructions(malicious)

        if run_real_llm:
            summary = await tracker.finalize(
                run_id=real_llm_run_id, task_ids=[t["task_id"] for t in tasks]
            )
            print(f"\nReal-LLM run {real_llm_run_id} persisted. Summary: {summary}")
            print(
                f"Inspect/compare later with: uv run python eval/report.py "
                f"--run-id {real_llm_run_id}"
            )
    finally:
        await db.close()
        await graph.close()

    print_report(results, injection_ok, real_llm_results if run_real_llm else None)


def print_report(
    results: list[BaselineResult],
    injection_ok: bool | None,
    real_llm_results: list[RealLlmResult] | None,
) -> None:
    print(f"Tasks x scripted baselines: {len(results)} results\n")
    by_baseline: dict[str, list[BaselineResult]] = {}
    for r in results:
        by_baseline.setdefault(r.baseline, []).append(r)

    print("=== Baselines A-D and scripted E (no real LLM involved in the decision) ===")
    print(f"{'baseline':22} {'mean recall':12} {'mean precision':14}")
    for name, rs in sorted(by_baseline.items()):
        mean_recall = sum(r.recall for r in rs) / len(rs)
        mean_precision = sum(r.precision for r in rs) / len(rs)
        print(f"{name:22} {mean_recall:>10.1%}  {mean_precision:>12.1%}")

    print("\nPer-task, E_scripted_mechanics (tool-selection/citation SCRIPTED, not real):")
    for r in by_baseline.get("E_scripted_mechanics", []):
        print(
            f"  {r.task_id:20} recall={r.recall:.0%} precision={r.precision:.0%} "
            f"tool_calls={r.tool_call_count} termination={r.termination_reason}"
        )

    print("\nPer-task, baseline B (vector-only) -- where it falls short of C/D:")
    for r in by_baseline.get("B_vector_only", []):
        if r.recall < 1.0:
            print(f"  {r.task_id:20} recall={r.recall:.0%} precision={r.precision:.0%}")

    if real_llm_results is None:
        print(
            "\n=== REAL-LLM baseline: NOT RUN (LLM_PROVIDER=fake) ===\n"
            "No real-LLM numbers are reported below. Nothing above this line "
            "involved a real LLM making a tool-selection/citation decision."
        )
        _print_injection_result(injection_ok)
        return

    print("\n=== REAL-LLM baseline (E_real_llm) -- every tool-selection, ")
    print("multi-step-reasoning, termination and citation decision made by the ")
    print("actual configured LLM provider, NOT scripted ===\n")

    mean_recall = sum(r.recall for r in real_llm_results) / len(real_llm_results)
    mean_precision = sum(r.precision for r in real_llm_results) / len(real_llm_results)
    total_in = sum(r.input_tokens for r in real_llm_results)
    total_out = sum(r.output_tokens for r in real_llm_results)
    mean_latency = sum(r.latency_ms for r in real_llm_results) / len(real_llm_results)
    print(f"E_real_llm mean recall={mean_recall:.1%} mean precision={mean_precision:.1%}")
    print(
        f"E_real_llm total tokens: input={total_in} output={total_out} "
        f"(cost estimate requires a per-token price for the configured model, "
        f"not tracked in this codebase -- token counts above are the measured "
        f"figures; apply the provider's published price yourself if needed)"
    )
    print(f"E_real_llm mean latency: {mean_latency:.0f}ms\n")

    print("--- Per-task REAL-LLM results ---")
    for r in real_llm_results:
        print(f"\n[{r.task_id}] ({r.category})")
        if r.error:
            print(f"  ERROR: {r.error}")
            continue
        print(f"  answer: {r.answer!r}")
        print(
            f"  answer_contains_expected: {r.answer_contains_expected} "
            f"(heuristic substring check only, not an LLM-graded correctness score)"
        )
        print(f"  cited_memory_ids: {r.cited_memory_ids}  expected: {sorted(r.expected)}")
        print(f"  recall={r.recall:.0%} precision={r.precision:.0%}")
        print(f"  tool_sequence: {r.tool_sequence}  (tool_call_count={r.tool_call_count})")
        print(f"  termination_reason: {r.termination_reason}")
        print(
            f"  latency_ms={r.latency_ms:.0f} input_tokens={r.input_tokens} "
            f"output_tokens={r.output_tokens}"
        )

    _print_injection_result(injection_ok)


def _print_injection_result(injection_ok: bool | None) -> None:
    injection_label = "PASS" if injection_ok else "FAIL" if injection_ok is False else "not run"
    print(
        "\nPrompt injection (structural, retrieved content never treated as an "
        f"instruction): {injection_label}"
    )


if __name__ == "__main__":
    asyncio.run(main())
