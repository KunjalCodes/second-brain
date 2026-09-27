"""Phase 11 (M5): structured logs, correlation, access lines, metrics."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.logging import JsonFormatter, _ContextFilter
from app.core.metrics import metrics
from app.main import create_app

TOKEN = "t" * 40


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    metrics.reset()
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def json_logs() -> Iterator[list[dict]]:
    records: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(json.loads(self.format(record)))

    handler = Capture()
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def test_access_line_is_json_with_route_template_and_no_query(client, json_logs) -> None:  # noqa: ANN001
    r = client.get(
        "/memories/search",
        params={"q": "my secret diagnosis"},
        headers={"X-User-Id": "obs-user", "X-Request-ID": "cli-1"},
    )
    access = [e for e in json_logs if e.get("event") == "http_request"]
    assert access, json_logs
    line = access[-1]
    assert line["route"] == "/memories/search"
    assert line["method"] == "GET"
    assert line["status"] == r.status_code
    assert isinstance(line["latency_ms"], float)
    assert line["request_id"] == r.headers["X-Request-ID"]
    assert line["client_request_id"] == "cli-1"
    assert "secret diagnosis" not in json.dumps(json_logs)


def test_path_parameters_are_templated_not_logged(client, json_logs) -> None:  # noqa: ANN001
    client.get("/memories/abc-123-private-id", headers={"X-User-Id": "obs-user"})
    line = [e for e in json_logs if e.get("event") == "http_request"][-1]
    assert line["route"] == "/memories/{memory_id}"
    assert "abc-123-private-id" not in json.dumps(line)


def test_metrics_require_the_operator_token(client) -> None:  # noqa: ANN001
    assert client.get("/internal/metrics").status_code == 401
    assert client.get("/internal/metrics", headers={"X-Internal-Token": "wrong"}).status_code == 401


def test_metrics_report_requests_errors_and_latency(client) -> None:  # noqa: ANN001
    client.get("/health")
    client.get("/no/such/route")
    client.get("/memories/does-not-exist", headers={"X-User-Id": "obs-user"})
    r = client.get("/internal/metrics", headers={"X-Internal-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    http = body["http"]
    assert http["requests_total"] >= 3
    assert http["requests_by_status_class"]["4xx"] >= 2
    assert http["errors_by_code"].get("not_found", 0) >= 1
    assert "all" in http["latency_ms"]
    assert "queues" in body and "llm_providers" in body and "sweeper" in body
    assert "obs-user" not in r.text, "no user ids in metrics"
