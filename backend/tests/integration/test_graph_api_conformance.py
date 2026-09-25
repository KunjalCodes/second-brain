"""Runs the Phase 8 deterministic conformance harness (eval/graph_api_runner.py)
as part of the suite, unpersisted, so a regression in any explorer semantic
fails CI -- not only a manual eval run (PHASE8_PLAN.md section 15)."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from eval.graph_api_runner import run

pytestmark = pytest.mark.integration


async def test_every_conformance_task_passes(db, graph) -> None:  # noqa: ANN001
    out = await run(db, graph, get_settings(), persist=False)
    failed = [(r["task_id"], r["notes"]) for r in out["results"] if not r["passed"]]
    assert failed == []
    assert len(out["results"]) >= 39
    assert all(r["precision"] == 1.0 and r["recall"] == 1.0 for r in out["results"])
