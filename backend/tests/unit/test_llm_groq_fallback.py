# ruff: noqa: E501
"""Groq adapter (mocked HTTP), the primary-then-fallback client, and its wiring."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from app.core.errors import (
    ConfigurationError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.llm.base import LLMResult
from app.llm.fallback import FallbackLLMClient
from app.llm.providers.groq import GroqClient
from app.llm.resilience import reset_provider_health


class Out(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def _health() -> None:
    reset_provider_health()


def groq(handler, retries: int = 0) -> GroqClient:  # noqa: ANN001
    return GroqClient(
        api_key="k", model="m", prompt_version="v1", max_retries=retries,
        transport=httpx.MockTransport(handler),
    )


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })


async def test_groq_returns_validated_result_and_reports_itself() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return ok('{"answer": "hi"}')

    r = await groq(handler).complete_structured(prompt="p", schema=Out)
    assert r.value.answer == "hi" and r.provider == "groq" and r.model == "m"
    assert (r.input_tokens, r.output_tokens) == (3, 2)
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in seen["body"]["messages"][0]["content"]
    assert seen["auth"] == "Bearer k"


@pytest.mark.parametrize(
    ("resp", "error"),
    [
        (httpx.Response(401), ConfigurationError),
        (httpx.Response(429, headers={"retry-after": "1"}), LLMRateLimitedError),
        (httpx.Response(503), LLMUnavailableError),
        (httpx.Response(400, json={"error": {"code": "json_validate_failed"}}), LLMResponseInvalidError),
        (httpx.Response(400, json={"error": {"code": "other"}}), ConfigurationError),
        (ok("not json"), LLMResponseInvalidError),
    ],
)
async def test_groq_errors_map_into_the_taxonomy(resp: httpx.Response, error: type) -> None:
    with pytest.raises(error):
        await groq(lambda _r: resp).complete_structured(prompt="p", schema=Out)


async def test_groq_timeout_is_unavailable() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMUnavailableError):
        await groq(handler).complete_structured(prompt="p", schema=Out)


def test_groq_requires_key_and_model() -> None:
    with pytest.raises(ConfigurationError):
        GroqClient(api_key="", model="m", prompt_version="v")
    with pytest.raises(ConfigurationError):
        GroqClient(api_key="k", model="", prompt_version="v")


class Scripted:
    def __init__(self, outcome: object, provider: str) -> None:
        self.outcome, self.provider, self.calls = outcome, provider, 0

    async def complete_structured(self, **_kw):  # noqa: ANN003, ANN201
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return LLMResult(Out(answer="x"), self.provider, "m", "v", 0, 0, 1, "{}")


async def test_fallback_answers_when_primary_is_unavailable_or_throttled() -> None:
    for err in (LLMUnavailableError("t"), LLMRateLimitedError("r")):
        primary, backup = Scripted(err, "gemini"), Scripted(None, "groq")
        r = await FallbackLLMClient(primary, backup).complete_structured(prompt="p", schema=Out)
        assert r.provider == "groq" and primary.calls == 1 and backup.calls == 1


async def test_fallback_is_not_used_when_primary_works() -> None:
    primary, backup = Scripted(None, "gemini"), Scripted(None, "groq")
    r = await FallbackLLMClient(primary, backup).complete_structured(prompt="p", schema=Out)
    assert r.provider == "gemini" and backup.calls == 0


@pytest.mark.parametrize("err", [ConfigurationError("bad key"), LLMResponseInvalidError("bad json")])
async def test_fallback_never_hides_config_or_invalid_output(err: Exception) -> None:
    primary, backup = Scripted(err, "gemini"), Scripted(None, "groq")
    with pytest.raises(type(err)):
        await FallbackLLMClient(primary, backup).complete_structured(prompt="p", schema=Out)
    assert backup.calls == 0


async def test_when_both_fail_the_primary_error_is_raised() -> None:
    primary = Scripted(LLMRateLimitedError("primary limited"), "gemini")
    backup = Scripted(LLMUnavailableError("backup down"), "groq")
    with pytest.raises(LLMRateLimitedError):
        await FallbackLLMClient(primary, backup).complete_structured(prompt="p", schema=Out)


def _settings(**env: str):  # noqa: ANN202
    from app.core.config import Settings

    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_factory_builds_a_fallback_chain_only_when_configured() -> None:
    from app.llm.factory import build_llm_client
    from app.llm.providers.gemini import GeminiClient

    single = build_llm_client(_settings(llm_provider="groq", llm_model="m", groq_api_key="k"))
    assert isinstance(single, GroqClient)
    chain = build_llm_client(_settings(
        llm_provider="groq", llm_model="m", groq_api_key="k",
        llm_fallback_provider="gemini", llm_fallback_model="g", gemini_api_key="gk",
    ))
    assert isinstance(chain, FallbackLLMClient)
    assert isinstance(chain._primary, GroqClient) and isinstance(chain._fallback, GeminiClient)  # noqa: SLF001


def test_fallback_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="differ"):
        _settings(llm_provider="groq", llm_fallback_provider="groq")
    with pytest.raises(ValueError, match="LLM_FALLBACK_PROVIDER"):
        _settings(llm_provider="groq", llm_fallback_provider="fake")
