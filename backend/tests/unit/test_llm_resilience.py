"""Phase 11 (M3): provider retry / Retry-After / cooldown policy, and the
Gemini adapters' mapping of one attempt's failure into the taxonomy.

No network: attempts are scripted, and `sleep`/`clock`/`rng` are injected so
waits are asserted exactly instead of slept.
"""

from __future__ import annotations

import pytest
from google.genai import errors as genai_errors

from app.core.errors import (
    ConfigurationError,
    EmbeddingRateLimitedError,
    EmbeddingUnavailableError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.embeddings.providers.gemini import _map_gemini_error as map_embedding_error
from app.llm.providers._gemini_errors import retry_after_seconds
from app.llm.providers.gemini import _map_gemini_error
from app.llm.resilience import (
    RetryPolicy,
    call_with_resilience,
    provider_health,
    reset_provider_health,
)


@pytest.fixture(autouse=True)
def _fresh_health():  # noqa: ANN202
    reset_provider_health()
    yield
    reset_provider_health()


class Script:
    """Raises the queued errors in order, then returns 'ok'."""

    def __init__(self, *outcomes: BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.outcomes:
            raise self.outcomes.pop(0)
        return "ok"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


async def _run(script: Script, clock: Clock, policy: RetryPolicy | None = None, **kw):  # noqa: ANN003, ANN202
    return await call_with_resilience(
        "p",
        script,
        policy=policy or RetryPolicy(),
        sleep=clock.sleep,
        clock=clock,
        rng=lambda: 1.0,
        **kw,
    )


async def test_429_with_retry_after_waits_exactly_that_long() -> None:
    clock = Clock()
    script = Script(LLMRateLimitedError("429", retry_after_seconds=7))
    assert await _run(script, clock) == "ok"
    assert clock.waits == [7]


async def test_429_without_hint_uses_bounded_exponential_backoff() -> None:
    clock = Clock()
    script = Script(LLMRateLimitedError("429"), LLMRateLimitedError("429"))
    assert await _run(script, clock, RetryPolicy(cooldown_after_consecutive_429=5)) == "ok"
    assert clock.waits == [1.0, 2.0]


async def test_retry_after_longer_than_max_wait_gives_up_keeping_the_hint() -> None:
    clock = Clock()
    script = Script(LLMRateLimitedError("429", retry_after_seconds=120))
    with pytest.raises(LLMRateLimitedError) as info:
        await _run(script, clock)
    assert info.value.retry_after_seconds == 120
    assert clock.waits == [] and script.calls == 1


@pytest.mark.parametrize(
    "exc",
    [
        LLMUnavailableError("Gemini did not respond within 30s"),  # timeout
        LLMUnavailableError("Gemini could not be reached"),  # connection error
        LLMUnavailableError("Gemini is unavailable (HTTP 500)"),  # 5xx
    ],
)
async def test_transient_failures_are_retried(exc: Exception) -> None:
    clock = Clock()
    script = Script(exc)
    assert await _run(script, clock) == "ok"
    assert script.calls == 2


@pytest.mark.parametrize(
    "exc",
    [LLMResponseInvalidError("bad json"), ConfigurationError("bad key")],
)
async def test_invalid_output_and_configuration_errors_are_never_retried(exc: Exception) -> None:
    clock = Clock()
    script = Script(exc, exc)
    with pytest.raises(type(exc)):
        await _run(script, clock)
    assert script.calls == 1 and clock.waits == []


async def test_exhausted_budget_raises_the_last_error_and_is_bounded() -> None:
    clock = Clock()
    script = Script(*[LLMUnavailableError("down")] * 10)
    with pytest.raises(LLMUnavailableError):
        await _run(script, clock, RetryPolicy(max_retries=3))
    assert script.calls == 4, "max_retries + 1 attempts, never more"
    assert all(w <= 8.0 for w in clock.waits)


async def test_repeated_429_trips_cooldown_and_later_calls_fail_fast() -> None:
    clock = Clock()
    policy = RetryPolicy(cooldown_after_consecutive_429=3, cooldown_base_seconds=60)
    script = Script(*[LLMRateLimitedError("429")] * 3)
    with pytest.raises(LLMRateLimitedError) as info:
        await _run(script, clock, policy)
    assert info.value.retry_after_seconds == 60
    assert script.calls == 3

    # While cooling down: no call reaches the provider.
    skipped = Script()
    with pytest.raises(LLMRateLimitedError):
        await _run(skipped, clock, policy)
    assert skipped.calls == 0

    # After the window: calls resume, and success resets the breaker.
    clock.now += 61
    ok = Script()
    assert await _run(ok, clock, policy) == "ok"
    assert provider_health("p").cooldown_trips == 0


async def test_embedding_cooldown_raises_the_embedding_error_type() -> None:
    clock = Clock()
    policy = RetryPolicy(cooldown_after_consecutive_429=1)
    script = Script(EmbeddingRateLimitedError("429"))
    with pytest.raises(EmbeddingRateLimitedError):
        await _run(
            script,
            clock,
            policy,
            rate_limited_error=lambda m, a: EmbeddingRateLimitedError(m, retry_after_seconds=a),
        )


# -- Gemini mapping of ONE attempt ------------------------------------------------


def _api_error(code: int, details: list | None = None) -> genai_errors.APIError:
    body = {"error": {"message": "secret internal text", "details": details or []}}
    return genai_errors.APIError(code, body)


def test_429_maps_to_rate_limited_with_retry_info_hint() -> None:
    exc = _api_error(
        429, [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "13s"}]
    )
    mapped = _map_gemini_error(exc)
    assert isinstance(mapped, LLMRateLimitedError)
    assert mapped.retry_after_seconds == 13
    assert mapped.headers == {"Retry-After": "13"}
    assert isinstance(map_embedding_error(exc), EmbeddingRateLimitedError)


def test_retry_after_header_is_read_when_present() -> None:
    class Resp:
        headers = {"retry-after": "4"}

    exc = _api_error(429)
    exc.response = Resp()  # type: ignore[assignment]
    assert retry_after_seconds(exc) == 4


def test_transport_errors_are_mapped_and_messages_leak_nothing() -> None:
    import httpx

    mapped = _map_gemini_error(httpx.ConnectError("dns failure for internal-host:443"))
    assert isinstance(mapped, LLMUnavailableError)
    assert "internal-host" not in mapped.message
    emb = map_embedding_error(ConnectionResetError("reset by peer 10.0.0.1"))
    assert isinstance(emb, EmbeddingUnavailableError) and "10.0.0.1" not in emb.message
    other = _map_gemini_error(RuntimeError("stack detail /Users/x/secret"))
    assert "/Users/" not in other.message
    assert "secret internal text" not in _map_gemini_error(_api_error(503)).message
