"""Phase 11 (M3): provider failures in background extraction, end to end.

* throttling (429 / cooldown) defers a job WITHOUT consuming an attempt;
* past `llm_throttle_max_age_seconds` it counts as a normal failed attempt,
  so the job still terminates;
* no LLM failure class ever writes to Neo4j (canonical memory untouched).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import (
    ConfigurationError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.llm.fake import FakeLLMClient
from app.models.extraction import ExtractionJobStatus
from tests.conftest import JAN
from tests.integration.test_job_reliability import (
    _job,
    _memory_count,
    _message_job,
    _settings,
    _tick,
)

pytestmark = pytest.mark.integration
CONTENT = "I use Grafana at work every single day."


async def test_throttling_defers_without_consuming_attempts(
    db, graph, user_id, pg_cleanup, cleanup  # noqa: ANN001
) -> None:
    settings = _settings(extraction_max_attempts=2)
    await _message_job(db, settings, user_id, CONTENT)
    llm = FakeLLMClient(failure=LLMRateLimitedError("429", retry_after_seconds=90))

    now = JAN
    for _ in range(5):  # far more throttled rounds than max_attempts
        await _tick(db, graph, settings, llm, now=now)
        job = await _job(db, user_id)
        assert job.status is ExtractionJobStatus.FAILED
        assert job.attempts == 0, "throttling must not spend retry attempts"
        assert job.error_code == "llm_rate_limited"
        assert job.next_attempt_at == now + timedelta(seconds=90)
        now = job.next_attempt_at
    assert await _memory_count(graph, user_id) == 0


async def test_throttling_past_max_age_becomes_a_terminal_failure(
    db, graph, user_id, pg_cleanup, cleanup  # noqa: ANN001
) -> None:
    settings = _settings(extraction_max_attempts=1, llm_throttle_max_age_seconds=3600)
    await _message_job(db, settings, user_id, CONTENT)
    llm = FakeLLMClient(failure=LLMRateLimitedError("429"))
    await _tick(db, graph, settings, llm, now=JAN + timedelta(hours=2))
    job = await _job(db, user_id)
    assert job.status is ExtractionJobStatus.ABANDONED
    assert job.error_code == "llm_unavailable"


@pytest.mark.parametrize(
    "failure",
    [
        LLMRateLimitedError("429"),
        LLMUnavailableError("timeout"),
        LLMUnavailableError("could not be reached"),
        LLMUnavailableError("HTTP 500"),
        LLMResponseInvalidError("bad json"),
        ConfigurationError("bad key"),
    ],
    ids=["429", "timeout", "network", "5xx", "invalid", "config"],
)
async def test_no_llm_failure_writes_to_neo4j(
    db, graph, user_id, pg_cleanup, cleanup, failure  # noqa: ANN001
) -> None:
    settings = _settings()
    await _message_job(db, settings, user_id, CONTENT)
    await _tick(db, graph, settings, FakeLLMClient(failure=failure), now=JAN)
    assert await _memory_count(graph, user_id) == 0
    job = await _job(db, user_id)
    assert job.status in (ExtractionJobStatus.FAILED, ExtractionJobStatus.ABANDONED)
    if isinstance(failure, LLMResponseInvalidError | ConfigurationError):
        assert job.status is ExtractionJobStatus.ABANDONED
