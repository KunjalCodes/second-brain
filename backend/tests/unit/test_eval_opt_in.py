"""Phase 11 (M3): real-LLM evaluation never runs unless explicitly requested."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from eval.opt_in import is_rate_limit_error, real_llm_requested


def _settings(provider: str) -> Settings:
    return Settings(llm_provider=provider, llm_model="m", gemini_api_key="k")


def test_configured_provider_alone_does_not_run_real_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_REAL_LLM", raising=False)
    run, why = real_llm_requested(_settings("gemini"), argv=[])
    assert run is False and "opt-in" in why


def test_flag_or_env_with_real_provider_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_REAL_LLM", raising=False)
    assert real_llm_requested(_settings("gemini"), argv=["--real-llm"])[0] is True
    monkeypatch.setenv("EVAL_REAL_LLM", "1")
    assert real_llm_requested(_settings("gemini"), argv=[])[0] is True


def test_fake_provider_never_runs_even_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_REAL_LLM", "1")
    assert real_llm_requested(_settings("fake"), argv=["--real-llm"])[0] is False


def test_runners_gate_through_the_helper_only() -> None:
    """Guards against re-introducing the implicit `llm_provider != "fake"` gate."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "eval"
    for name in ("agent_runner.py", "document_runner.py"):
        source = (root / name).read_text()
        assert "real_llm_requested(settings)" in source, name
        assert 'run_real_llm = settings.llm_provider != "fake"' not in source, name


def test_rate_limit_classification_is_type_based_and_backward_compatible() -> None:
    assert is_rate_limit_error("LLMRateLimitedError: Gemini is rate limiting requests (HTTP 429)")
    assert is_rate_limit_error("LLMUnavailableError: Gemini is unavailable (HTTP 429)")  # old rows
    assert not is_rate_limit_error("LLMUnavailableError: Gemini could not be reached")
