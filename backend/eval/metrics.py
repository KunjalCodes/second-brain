"""Evaluation metrics (docs/EVALUATION.md sections 6-9).

Pure functions over a list of per-scenario results. Kept separate from `runner.py`
so metrics computation can be tested and reused independent of how results were
produced (deterministic harness today, a live-provider harness later).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    category: str
    expected_decisions: list[str]
    actual_decisions: list[str]
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class EvaluationReport:
    results: list[ScenarioResult]
    decision_accuracy: float
    confusion: dict[tuple[str, str], int]  # (expected, actual) -> count
    category_accuracy: dict[str, float]
    wrong_merge_count: int = 0

    def summary(self) -> str:
        lines = [
            f"Scenarios: {len(self.results)}",
            f"Decision accuracy: {self.decision_accuracy:.1%}",
            f"Wrong-merge count: {self.wrong_merge_count} (target: 0, reported "
            f"separately -- never averaged into accuracy, per docs/EVALUATION.md)",
            "",
            "By category:",
        ]
        for category, acc in sorted(self.category_accuracy.items()):
            lines.append(f"  {category:22} {acc:.1%}")
        lines.append("")
        lines.append("Failures:")
        failures = [r for r in self.results if not r.passed]
        if not failures:
            lines.append("  (none)")
        for r in failures:
            lines.append(
                f"  {r.scenario_id}: expected {r.expected_decisions}, "
                f"got {r.actual_decisions} -- {r.detail}"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class RetrievalScenarioResult:
    """One retrieval scenario's outcome (PHASE3_PLAN.md section 16). `retrieved`
    and `expected` are memory ids -- `retrieved` in rank order, `expected`
    unordered (the gold relevant set).
    """

    scenario_id: str
    category: str
    retrieved: list[str]
    expected: set[str]
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank: float
    detail: str = ""


@dataclass(frozen=True)
class RetrievalEvaluationReport:
    results: list[RetrievalScenarioResult]
    mean_recall_at_k: float
    mean_precision_at_k: float
    mean_reciprocal_rank: float
    category_recall: dict[str, float]

    def summary(self) -> str:
        lines = [
            f"Scenarios: {len(self.results)}",
            f"Mean Recall@K: {self.mean_recall_at_k:.1%}",
            f"Mean Precision@K: {self.mean_precision_at_k:.1%}",
            f"Mean Reciprocal Rank (MRR): {self.mean_reciprocal_rank:.3f}",
            "",
            "By category (mean Recall@K):",
        ]
        for category, recall in sorted(self.category_recall.items()):
            lines.append(f"  {category:20} {recall:.1%}")
        return "\n".join(lines)


def recall_at_k(retrieved: list[str], expected: set[str]) -> float:
    """Of everything relevant, how much was found (anywhere in `retrieved`)?
    1.0 if `expected` is empty (nothing to miss).
    """
    if not expected:
        return 1.0
    found = expected & set(retrieved)
    return len(found) / len(expected)


def precision_at_k(retrieved: list[str], expected: set[str]) -> float:
    """Of what was returned, how much was actually relevant? 1.0 for an empty
    `retrieved` (nothing wrong was returned, even though nothing was returned).
    """
    if not retrieved:
        return 1.0
    relevant = expected & set(retrieved)
    return len(relevant) / len(retrieved)


def reciprocal_rank(retrieved: list[str], expected: set[str]) -> float:
    """1 / (rank of the first relevant result), 0.0 if none of `retrieved` is
    relevant -- the standard MRR per-query term.
    """
    for i, memory_id in enumerate(retrieved, start=1):
        if memory_id in expected:
            return 1.0 / i
    return 0.0


def build_retrieval_report(results: list[RetrievalScenarioResult]) -> RetrievalEvaluationReport:
    """Aggregate per-scenario retrieval results. No fabricated values -- every
    number is computed directly from `results` (EVALUATION.md section 20).
    """
    if not results:
        return RetrievalEvaluationReport(
            results=[], mean_recall_at_k=0.0, mean_precision_at_k=0.0,
            mean_reciprocal_rank=0.0, category_recall={},
        )

    by_category: dict[str, list[RetrievalScenarioResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    return RetrievalEvaluationReport(
        results=results,
        mean_recall_at_k=sum(r.recall_at_k for r in results) / len(results),
        mean_precision_at_k=sum(r.precision_at_k for r in results) / len(results),
        mean_reciprocal_rank=sum(r.reciprocal_rank for r in results) / len(results),
        category_recall={
            cat: sum(r.recall_at_k for r in rs) / len(rs) for cat, rs in by_category.items()
        },
    )


def build_report(results: list[ScenarioResult]) -> EvaluationReport:
    """Aggregate per-scenario results into a report. No fabricated values: every
    number here is computed directly from `results` (docs/EVALUATION.md section 20
    -- "Do not use illustrative values in the final report").
    """
    if not results:
        return EvaluationReport(
            results=[], decision_accuracy=0.0, confusion={}, category_accuracy={}
        )

    confusion: Counter[tuple[str, str]] = Counter()
    for r in results:
        expected = r.expected_decisions[0] if r.expected_decisions else "NONE"
        actual = r.actual_decisions[0] if r.actual_decisions else "NONE"
        confusion[(expected, actual)] += 1

    overall_correct = sum(1 for r in results if r.passed)
    decision_accuracy = overall_correct / len(results)

    by_category: dict[str, list[ScenarioResult]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)
    category_accuracy = {
        cat: sum(1 for r in rs if r.passed) / len(rs) for cat, rs in by_category.items()
    }

    return EvaluationReport(
        results=results,
        decision_accuracy=decision_accuracy,
        confusion=dict(confusion),
        category_accuracy=category_accuracy,
    )
