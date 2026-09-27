"""Phase 11 (S9): baseline security headers on API responses."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_baseline_headers_on_every_response() -> None:
    with TestClient(create_app()) as c:
        for r in (c.get("/health"), c.get("/no/such/route")):
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert "camera=()" in r.headers["permissions-policy"]


def test_hsts_only_where_https_is_guaranteed(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(create_app()) as c:
        assert "strict-transport-security" not in c.get("/health").headers

    for key, value in {
        "APP_ENV": "staging",
        "ALLOW_USER_ID_HEADER": "false",
        "FRONTEND_URL": "https://brain.example.com",
        "AUTH_SCRYPT_N": "32768",
        "AUTH_SCRYPT_P": "3",
        "INTERNAL_API_TOKEN": "t" * 40,
        "ALLOW_FAKE_AI": "true",
        "DOCUMENT_STORAGE_DIR": "/tmp/second-brain-docs",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        r = c.get("/health")
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
    # D8: interactive docs are hidden in staging.
    with TestClient(create_app()) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
