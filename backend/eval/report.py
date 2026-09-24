"""Evaluation-run report aggregation (Phase 7, PHASE7_PLAN.md section 6,
Core Objective points 1/2/8: "what was evaluated?", "can results be
historically compared across runs?").

A read-side layer over the two Phase 7 tables (`evaluation_runs`,
`evaluation_task_results`) -- it does not re-run any harness, only reports
on what `eval/persistence.py`'s `EvaluationRunTracker` already persisted.
Every number printed here is read directly from a stored row; nothing is
recomputed or estimated (docs/EVALUATION.md section 20's "no illustrative
values" discipline, applied to historical reporting).

Usage:
    NEO4J_URI=... DATABASE_URL=... uv run python eval/report.py --run-id <id>
    NEO4J_URI=... DATABASE_URL=... uv run python eval/report.py --harness document --latest
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.models.evaluation import EvaluationRun, EvaluationTaskResult  # noqa: E402
from app.repositories.evaluation_repository import (  # noqa: E402
    EvaluationRunRepository,
    EvaluationTaskResultRepository,
)

#: How much a category's mean recall/precision may drop versus the
#: immediately preceding same-configuration run before this report flags
#: it as a regression -- a fixed, documented tolerance (never versus an
#: arbitrary historical best, which would reward a lucky run instead of
#: penalizing a real decline).
_REGRESSION_TOLERANCE = 0.05


def _format_status_line(run: EvaluationRun, tasks: list[EvaluationTaskResult]) -> str:
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
    counts = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
    return (
        f"Run {run.id}\n"
        f"  harness={run.harness} dataset={run.dataset_version} mode={run.mode}\n"
        f"  provider={run.llm_provider or 'n/a'} model={run.llm_model or 'n/a'}\n"
        f"  status={run.status.value} total_tasks={run.total_tasks}\n"
        f"  git_commit={run.git_commit or 'unknown'}\n"
        f"  created_at={run.created_at} started_at={run.started_at} "
        f"completed_at={run.completed_at}\n"
        f"  task counts: {counts or '(none recorded)'}"
    )


def _format_summary(summary: dict | None) -> str:
    if summary is None:
        return "  (no summary persisted yet)"
    lines = ["  summary:"]
    for key, value in summary.items():
        if key == "incomplete_task_ids" and not value:
            continue
        lines.append(f"    {key}: {value if value is not None else 'N/A'}")
    return "\n".join(lines)


def _format_task_detail(tasks: list[EvaluationTaskResult]) -> str:
    lines = ["  tasks:"]
    for t in sorted(tasks, key=lambda r: r.task_id):
        bits = [f"status={t.status.value}"]
        if t.passed is not None:
            bits.append(f"passed={t.passed}")
        if t.error_detail:
            bits.append(f"error={t.error_detail!r}")
        lines.append(f"    {t.task_id:35} ({t.category}) {' '.join(bits)}")
    return "\n".join(lines)


def _format_regression(run: EvaluationRun, previous: EvaluationRun | None) -> str:
    if previous is None:
        return "  regression check: (no prior COMPLETED run of this exact configuration)"
    if run.summary is None or previous.summary is None:
        return "  regression check: N/A (missing summary on this or the prior run)"
    lines = [f"  regression check vs. run {previous.id} ({previous.created_at}):"]
    flagged = False
    for key in ("mean_recall", "mean_precision"):
        current = run.summary.get(key)
        prior = previous.summary.get(key)
        if current is None or prior is None:
            lines.append(f"    {key}: N/A (missing on one of the two runs)")
            continue
        delta = current - prior
        marker = ""
        if delta < -_REGRESSION_TOLERANCE:
            marker = "  <-- REGRESSION"
            flagged = True
        lines.append(f"    {key}: {prior:.1%} -> {current:.1%} (delta {delta:+.1%}){marker}")
    if not flagged:
        lines.append(
            f"    no regression beyond the documented tolerance ({_REGRESSION_TOLERANCE:.0%})"
        )
    return "\n".join(lines)


async def print_run_report(db: Database, *, run_id: str) -> int:
    async with db.session() as session:
        run = await EvaluationRunRepository(session).get(run_id=run_id)
        if run is None:
            print(f"No such run: {run_id}", file=sys.stderr)
            return 1
        tasks = await EvaluationTaskResultRepository(session).list_for_run(run_id=run_id)
        previous = await EvaluationRunRepository(session).find_previous_completed(
            harness=run.harness,
            dataset_version=run.dataset_version,
            mode=run.mode,
            before=run.created_at,
        )

    print(_format_status_line(run, tasks))
    print(_format_summary(run.summary))
    print(_format_task_detail(tasks))
    print(_format_regression(run, previous))
    return 0


async def print_latest(db: Database, *, harness: str) -> int:
    async with db.session() as session:
        runs = await EvaluationRunRepository(session).list_recent(harness=harness, limit=1)
    if not runs:
        print(f"No runs recorded yet for harness={harness!r}", file=sys.stderr)
        return 1
    return await print_run_report(db, run_id=runs[0].id)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="A specific evaluation_runs.id to report on")
    parser.add_argument(
        "--harness", help="Report the most recent run for this harness (with --latest)"
    )
    parser.add_argument(
        "--latest", action="store_true", help="Use the most recent run for --harness"
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_configured:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 2

    db = Database(settings)
    await db.connect()
    try:
        if args.run_id:
            return await print_run_report(db, run_id=args.run_id)
        if args.latest and args.harness:
            return await print_latest(db, harness=args.harness)
        print("Pass --run-id <id>, or --harness <name> --latest.", file=sys.stderr)
        return 2
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
