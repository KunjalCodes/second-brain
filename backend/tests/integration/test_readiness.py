"""Phase 11 (M4): readiness checks dependencies AND the schema revision."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.routes import health
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fresh_cache():  # noqa: ANN202
    health.reset_migration_cache()
    yield
    health.reset_migration_cache()


def test_ready_when_databases_answer_and_schema_is_at_head(db, graph) -> None:  # noqa: ANN001
    with TestClient(create_app()) as client:
        r = client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "ready",
        "checks": {"neo4j": "ok", "postgresql": "ok", "migrations": "ok"},
    }


def test_not_ready_when_schema_revision_does_not_match_the_code(
    db, graph, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    # The code expects a revision the database is not at -- e.g. a deploy that
    # skipped `alembic upgrade head`.
    monkeypatch.setattr(health, "expected_migration_head", lambda: "a_future_revision")
    with TestClient(create_app()) as client:
        r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["migrations"] == "mismatch"
