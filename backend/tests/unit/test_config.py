"""Configuration: environment parsing and deployment guard-rails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings


def _settings(**env: str) -> Settings:
    return Settings(**env)  # type: ignore[arg-type]


class TestCorsOrigins:
    def test_combines_frontend_url_and_cors_origins(self) -> None:
        s = _settings(
            frontend_url="https://brain.vercel.app",
            cors_origins="http://localhost:3000,https://staging.example.com",
        )
        assert s.allowed_origins == [
            "https://brain.vercel.app",
            "http://localhost:3000",
            "https://staging.example.com",
        ]

    def test_strips_whitespace_and_trailing_slashes(self) -> None:
        s = _settings(cors_origins=" https://a.example.com/ , https://b.example.com ")
        assert s.allowed_origins == ["https://a.example.com", "https://b.example.com"]

    def test_deduplicates(self) -> None:
        s = _settings(
            frontend_url="https://a.example.com",
            cors_origins="https://a.example.com,https://b.example.com",
        )
        assert s.allowed_origins == ["https://a.example.com", "https://b.example.com"]

    def test_empty_configuration_yields_no_origins(self) -> None:
        assert _settings(frontend_url="", cors_origins="").allowed_origins == []

    def test_vercel_previews_are_off_by_default(self) -> None:
        """Phase 9 (PHASE9_PLAN.md F7): the old default admitted every
        *.vercel.app site as a credentialed origin."""
        assert _settings().allowed_origin_regex is None

    def test_vercel_previews_require_a_named_project(self) -> None:
        with pytest.raises(PydanticValidationError, match="CORS_VERCEL_PROJECT"):
            _settings(cors_allow_vercel_previews=True)
        s = _settings(cors_allow_vercel_previews=True, cors_vercel_project="brain")
        assert s.allowed_origin_regex == r"https://brain(-[a-z0-9-]+)?\.vercel\.app"

    def test_vercel_preview_regex_can_be_disabled(self) -> None:
        assert _settings(cors_allow_vercel_previews=False).allowed_origin_regex is None


class TestNeo4jConfiguration:
    def test_reports_unconfigured_when_values_missing(self) -> None:
        """There is no localhost default: an unset URI must read as unconfigured."""
        s = _settings(neo4j_uri="", neo4j_username="", neo4j_password="")
        assert s.neo4j_configured is False

    def test_requires_all_three_values(self) -> None:
        s = _settings(
            neo4j_uri="neo4j+s://x.databases.neo4j.io",
            neo4j_username="neo4j",
            neo4j_password="",
        )
        assert s.neo4j_configured is False

    def test_reports_configured_when_complete(self) -> None:
        s = _settings(
            neo4j_uri="neo4j+s://x.databases.neo4j.io",
            neo4j_username="neo4j",
            neo4j_password="secret",
        )
        assert s.neo4j_configured is True

    def test_transaction_retry_window_is_short_enough_for_http(self) -> None:
        """The driver default of 30s would hang a request through an outage."""
        assert _settings().neo4j_max_transaction_retry_time <= 10.0


class TestProductionGuards:
    def test_production_rejects_the_dev_identity_header(self) -> None:
        """The X-User-Id placeholder must never be live in production."""
        with pytest.raises(PydanticValidationError, match="ALLOW_USER_ID_HEADER"):
            _settings(app_env="production", allow_user_id_header=True)

    def test_production_is_allowed_with_the_header_disabled(self) -> None:
        # Phase 9: a production-like environment also needs an explicit browser
        # origin allow-list (tests/unit/test_auth_config.py covers every guard).
        s = _settings(
            app_env="production",
            allow_user_id_header=False,
            frontend_url="https://brain.example.com",
            auth_scrypt_n=2**15,
            auth_scrypt_p=3,
        )
        assert s.app_env == "production"

    def test_development_allows_the_header(self) -> None:
        s = _settings(app_env="development", allow_user_id_header=True)
        assert s.app_env == "development"


class TestMisc:
    def test_log_level_is_upper_cased(self) -> None:
        assert _settings(log_level="debug").log_level == "DEBUG"

    def test_hop_limit_is_bounded(self) -> None:
        with pytest.raises(PydanticValidationError):
            _settings(graph_max_hops=99)

    def test_settings_are_cached(self) -> None:
        assert get_settings() is get_settings()
