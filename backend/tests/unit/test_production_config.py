"""Phase 11 (M4): production-like startup refuses unsafe or broken settings,
while development keeps working on its defaults (docs/PHASE11_PLAN.md section 23)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings
from tests.unit.test_auth_unit import _prod


def test_a_complete_staging_configuration_starts() -> None:
    s = _prod(app_env="staging")
    assert s.docs_enabled is False
    assert s.effective_log_format == "json"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"database_url": ""}, "DATABASE_URL"),
        ({"neo4j_password": ""}, "NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD"),
        ({"internal_api_token": ""}, "INTERNAL_API_TOKEN"),
        ({"internal_api_token": "short"}, "INTERNAL_API_TOKEN"),
        ({"allow_fake_ai": False}, "ALLOW_FAKE_AI"),
        ({"llm_provider": "openai"}, "no adapter"),
        ({"llm_provider": "gemini", "gemini_api_key": ""}, "GEMINI_API_KEY and LLM_MODEL"),
        ({"document_storage_dir": "./data/documents"}, "DOCUMENT_STORAGE_DIR"),
        ({"debug": True}, "DEBUG"),
        ({"database_echo": True}, "DATABASE_ECHO"),
        ({"log_level": "DEBUG"}, "LOG_LEVEL"),
        ({"frontend_url": "http://brain.example.com"}, "must use https://"),
        (
            {
                "cors_allow_vercel_previews": True,
                "cors_vercel_project": "brain",
                "cors_vercel_team_scope": "me",
            },
            "CORS_ALLOW_VERCEL_PREVIEWS must be false",
        ),
    ],
)
def test_unsafe_production_setting_refuses_to_start(override: dict, message: str) -> None:
    with pytest.raises(PydanticValidationError, match=message):
        _prod(app_env="staging", **override)


def test_all_problems_are_reported_at_once() -> None:
    with pytest.raises(PydanticValidationError) as info:
        _prod(debug=True, database_echo=True, internal_api_token="")
    text = str(info.value)
    assert "DEBUG" in text and "DATABASE_ECHO" in text and "INTERNAL_API_TOKEN" in text


def test_localhost_origin_is_allowed_for_production_like_local_runs() -> None:
    assert _prod(frontend_url="http://localhost:3100").allowed_origins == ["http://localhost:3100"]


def test_docs_can_be_exposed_explicitly() -> None:
    assert _prod(expose_docs=True).docs_enabled is True


def test_development_defaults_still_start() -> None:
    s = Settings(app_env="development", allow_user_id_header=None)
    assert s.docs_enabled is True
    assert s.effective_log_format == "text"
    assert s.llm_provider == "fake" and s.internal_api_token == ""
