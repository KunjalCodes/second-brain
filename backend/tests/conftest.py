"""Shared test configuration.

Unit tests never touch a database. Integration tests are marked `integration` and
skip themselves when no Neo4j is reachable (see tests/integration/conftest.py).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.db.engine import reset_database
from app.graph.connection import reset_graph_client
from app.llm.factory import reset_llm_client
from app.storage.factory import reset_document_storage

#: Fixed instants used throughout the tests, so assertions are exact rather than
#: approximate. These mirror the seed narrative: EasyOCR from January, replaced by
#: PaddleOCR in May.
JAN = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
MAR = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
MAY = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
JUN = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _test_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:  # noqa: ANN001
    """Give every test a clean, explicit configuration.

    `get_settings` is cached for the process, so the cache is cleared before and
    after each test; otherwise the first test's environment would leak into all the
    others.
    """
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("NEO4J_URI", os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USERNAME", os.environ.get("NEO4J_USERNAME", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.environ.get("NEO4J_PASSWORD", "password"))
    monkeypatch.setenv("NEO4J_DATABASE", os.environ.get("NEO4J_DATABASE", "neo4j"))
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgresql+asyncpg://localhost/ai_second_brain_test"),
    )
    monkeypatch.setenv("DEV_USER_ID", "test-user")
    monkeypatch.setenv("ALLOW_USER_ID_HEADER", "true")
    # Phase 9: the pre-existing suite runs in the explicit dev-header mode (the
    # ALLOW_USER_ID_HEADER line above); session-mode tests switch per test.
    # Rate limiting needs PostgreSQL and would couple unrelated tests through
    # shared counters, so it is off by default and enabled by the tests that
    # exercise it. scrypt cost is lowered for speed -- the production floor is
    # enforced only for production-like APP_ENV (tests/unit/test_auth_config.py).
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    # Phase 11: the in-process sweeper would claim jobs concurrently with the
    # tests that drive the queue deterministically; tests that exercise it
    # construct a `Sweeper` explicitly.
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("AUTH_SCRYPT_N", "1024")
    monkeypatch.setenv("AUTH_SCRYPT_P", "1")
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    # Phase 2: FakeLLMClient is the default provider, so the unit suite never
    # needs an API key or network access.
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    # Phase 6: an isolated, per-test directory -- never the real
    # `./data/documents` default -- so a test exercising the process-wide
    # `get_document_storage()` singleton (e.g. through the API routes) cannot
    # collide with another test or leave real files behind.
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    get_settings.cache_clear()
    reset_graph_client()
    reset_database()
    reset_llm_client()
    reset_document_storage()
    yield
    get_settings.cache_clear()
    reset_graph_client()
    reset_database()
    reset_llm_client()
    reset_document_storage()


class FrozenClock:
    """A controllable clock, so temporal assertions are deterministic."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance_to(self, moment: datetime) -> None:
        self.now = moment


class SequentialIds:
    """Predictable id generator so tests can assert on exact stored ids."""

    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n}"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(JAN)


@pytest.fixture
def ids() -> SequentialIds:
    return SequentialIds()
