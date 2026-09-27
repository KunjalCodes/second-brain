"""Phase 11: an operator can recover jobs abandoned by a provider outage."""

from __future__ import annotations

import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.errors import LLMUnavailableError
from app.llm.fake import FakeLLMClient
from app.models.conversation import ExtractionStatus
from app.models.extraction import ExtractionJobStatus, ExtractionResult
from app.repositories.message_repository import MessageRepository
from tests.conftest import JAN
from tests.integration.test_job_reliability import (
    _candidate,
    _job,
    _memory_count,
    _message_job,
    _settings,
    _tick,
)

pytestmark = pytest.mark.integration
BACKEND = Path(__file__).resolve().parents[2]
CONTENT = "I use Grafana at work every single day."


def _requeue(*args: str) -> str:
    out = subprocess.run(
        ["uv", "run", "python", "../scripts/requeue_jobs.py", "--reason", "llm_unavailable", *args],
        cwd=BACKEND,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-1500:]
    return out.stdout


async def test_abandoned_after_outage_is_requeued_and_completes(
    db,
    graph,
    user_id,
    pg_cleanup,
    cleanup,  # noqa: ANN001
) -> None:
    settings = _settings(extraction_max_attempts=1)
    message = await _message_job(db, settings, user_id, CONTENT)
    await _tick(db, graph, settings, FakeLLMClient(failure=LLMUnavailableError("down")), now=JAN)
    job = await _job(db, user_id)
    assert job.status is ExtractionJobStatus.ABANDONED

    # completed_at is JAN (the test clock); widen the window accordingly.
    assert "dry run" in _requeue("--since-hours", "100000")
    assert (await _job(db, user_id)).status is ExtractionJobStatus.ABANDONED, (
        "dry run changes nothing"
    )
    assert "requeued" in _requeue("--since-hours", "100000", "--apply")
    job = await _job(db, user_id)
    assert (job.status, job.attempts) == (ExtractionJobStatus.FAILED, 0)

    good = FakeLLMClient(response=ExtractionResult(candidates=[_candidate(CONTENT, "Grafana")]))
    await _tick(db, graph, settings, good, now=JAN + timedelta(days=36500))
    assert (await _job(db, user_id)).status is ExtractionJobStatus.DONE
    assert await _memory_count(graph, user_id) == 1
    async with db.session() as session:
        stored = await MessageRepository(session, user_id=user_id).get(message_id=message.id)
    assert stored.extraction_status is ExtractionStatus.DONE
