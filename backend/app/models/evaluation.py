"""Persistent, resumable evaluation-run tracking (Phase 7, PHASE7_PLAN.md
section 6/7).

**Not canonical memory, not user data.** These models describe how well the
system performs on a dataset -- operational/developer metadata, the exact
same status `app.models.document`/`app.models.extraction` already have
relative to Neo4j (docs/adr/0006). No field here ever holds raw memory or
document content; only ids, counts, and scores (Phase 7's explicit security
requirement -- see `EvaluationTaskResult.metrics`'s own docstring).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EvaluationRunStatus(StrEnum):
    """A run stays resumable (`PENDING`/`IN_PROGRESS`) until every one of
    its `EvaluationTaskResult` rows reaches `COMPLETED` or `SKIPPED`, at
    which point `eval.persistence.EvaluationRunTracker.finalize` marks it
    `COMPLETED`. `FAILED` is reserved for the run itself crashing (e.g. a
    lost database connection) -- distinct from any individual task's
    outcome, which lives on `EvaluationTaskResult.status` instead.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationTaskStatus(StrEnum):
    """`COMPLETED` means the task ran to completion without an
    infrastructure error or rate-limit -- `EvaluationTaskResult.passed`
    (only meaningful then) is the separate, model-level judgment of
    whether the outcome was correct. This is deliberate: "which tasks
    failed because of the system" is `ERRORED`/`RATE_LIMITED`; "which
    tasks failed because of the LLM" is `COMPLETED` with `passed=False`.
    Never conflated (Phase 7's own stated Core Objective, points 3-4).
    """

    PENDING = "pending"
    COMPLETED = "completed"
    RATE_LIMITED = "rate_limited"
    ERRORED = "errored"
    SKIPPED = "skipped"


class EvaluationRun(BaseModel):
    """One resumable evaluation-harness invocation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    harness: str
    dataset_version: str
    mode: str  # "deterministic" | "real_llm"
    llm_provider: str | None = None
    llm_model: str | None = None
    status: EvaluationRunStatus
    total_tasks: int
    config: dict | None = None
    summary: dict | None = None
    git_commit: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class EvaluationTaskResult(BaseModel):
    """One task's outcome within one `EvaluationRun`.

    `metrics` holds whatever the harness computed for this task (recall,
    precision, tool sequence, cited memory ids, latency, tokens) -- only
    the keys actually available for that task; a key that was not computed
    is simply absent, never written as `0`/`null` standing in for
    "unknown" (explicit N/A semantics, never a fabricated zero).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    task_id: str
    category: str
    status: EvaluationTaskStatus
    passed: bool | None = None
    metrics: dict | None = None
    error_detail: str | None = Field(default=None, max_length=2000)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
