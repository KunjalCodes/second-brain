"""Phase 11 (M2): the app lifespan owns the sweeper's lifecycle."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.workers.sweeper import get_sweeper


def test_lifespan_starts_and_stops_the_sweeper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("SWEEP_INITIAL_DELAY_SECONDS", "600")  # never actually sweeps here
    get_settings.cache_clear()
    with TestClient(create_app()):
        sweeper = get_sweeper()
        assert sweeper is not None and sweeper.running
    assert get_sweeper() is None
    assert not sweeper.running


def test_sweeper_disabled_when_interval_is_zero() -> None:
    # tests/conftest.py sets SWEEP_INTERVAL_SECONDS=0 for every test.
    with TestClient(create_app()):
        assert get_sweeper() is None
