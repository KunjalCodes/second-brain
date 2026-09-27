"""Provider-neutral retry, Retry-After and cooldown policy for AI provider calls
(Phase 11, M3; docs/PHASE11_PLAN.md section 12).

Adapters (`app/llm/providers/*`, `app/embeddings/providers/*`) do ONE thing
provider-specific: run a single attempt and map any failure into the error
taxonomy below. Everything about *whether and when to try again* lives here,
so every provider behaves the same way:

| Failure (as mapped by the adapter)      | Behaviour                                   |
|-----------------------------------------|---------------------------------------------|
| `LLMRateLimitedError` (HTTP 429)        | honour `retry_after_seconds` if given and   |
|                                         | within `max_wait_seconds`, else jittered    |
|                                         | exponential backoff; repeated 429s start a  |
|                                         | process-wide cooldown for the provider      |
| `LLMUnavailableError` (timeout, 5xx,    | jittered exponential backoff                |
| connection/transport error)             |                                             |
| `LLMResponseInvalidError`               | never retried here (the extraction runner   |
|                                         | makes its one repair call itself)           |
| `ConfigurationError` (401/403/400...)   | never retried: raised immediately           |

Budget: at most `max_retries` retries (so `max_retries + 1` attempts) per call,
and no single wait longer than `max_wait_seconds`. When the budget runs out,
the LAST classified error is raised -- a rate-limit stays an
`LLMRateLimitedError` so background jobs can wait it out without spending a
job attempt (`ExtractionRunner._defer_for_provider`).

Cooldown: after `cooldown_after_consecutive_429` consecutive 429s the provider
is "cooling down" for `max(retry_after, cooldown_base_seconds)`, doubling on
each further trip up to `cooldown_max_seconds`. While cooling down, calls fail
fast with `LLMRateLimitedError` WITHOUT contacting the provider -- that is the
circuit breaker. Any success resets it. State is per process: with one
instance that is exact; with several it is conservative per instance.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.errors import (
    ConfigurationError,
    EmbeddingRateLimitedError,
    EmbeddingUnavailableError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_seconds: float = 1.0
    cap_seconds: float = 8.0
    max_wait_seconds: float = 30.0
    cooldown_after_consecutive_429: int = 3
    cooldown_base_seconds: float = 60.0
    cooldown_max_seconds: float = 900.0


@dataclass
class ProviderHealth:
    """Process-wide throttling state for one provider (also read by metrics)."""

    consecutive_429: int = 0
    cooldown_until: float = 0.0
    cooldown_trips: int = 0
    calls: int = 0
    successes: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def cooling_down(self, now: float) -> float:
        """Seconds of cooldown remaining (0 when not cooling down)."""
        return max(0.0, self.cooldown_until - now)


_HEALTH: dict[str, ProviderHealth] = {}


def provider_health(provider: str) -> ProviderHealth:
    return _HEALTH.setdefault(provider, ProviderHealth())


def all_provider_health() -> dict[str, ProviderHealth]:
    return dict(_HEALTH)


def reset_provider_health() -> None:
    """Tests only."""
    _HEALTH.clear()


def _classify(exc: BaseException) -> str:
    if isinstance(exc, LLMRateLimitedError | EmbeddingRateLimitedError):
        return "rate_limited"
    if isinstance(exc, LLMResponseInvalidError):
        return "invalid_output"
    if isinstance(exc, ConfigurationError):
        return "configuration"
    if isinstance(exc, LLMUnavailableError | EmbeddingUnavailableError):
        return "unavailable"
    return "unexpected"


async def call_with_resilience[T](
    provider: str,
    attempt: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[], float] = random.random,
    rate_limited_error: Callable[[str, float | None], Exception] | None = None,
) -> T:
    """Run `attempt` under the policy. `attempt` must raise only taxonomy
    errors (the adapter's mapping is responsible for that)."""
    health = provider_health(provider)
    make_limited = rate_limited_error or (
        lambda msg, after: LLMRateLimitedError(msg, retry_after_seconds=after)
    )

    remaining = health.cooling_down(clock())
    if remaining:
        health.errors["cooldown_skip"] = health.errors.get("cooldown_skip", 0) + 1
        raise make_limited(f"{provider} is rate limited; retry later", remaining)

    last: BaseException | None = None
    for attempt_no in range(policy.max_retries + 1):
        health.calls += 1
        try:
            result = await attempt()
        except (LLMResponseInvalidError, ConfigurationError) as exc:
            health.errors[_classify(exc)] = health.errors.get(_classify(exc), 0) + 1
            health.consecutive_429 = 0
            raise
        except (LLMUnavailableError, EmbeddingUnavailableError) as exc:
            kind = _classify(exc)
            health.errors[kind] = health.errors.get(kind, 0) + 1
            last = exc
            retry_after = getattr(exc, "retry_after_seconds", None)
            if kind == "rate_limited":
                health.consecutive_429 += 1
                if health.consecutive_429 >= policy.cooldown_after_consecutive_429:
                    health.cooldown_trips += 1
                    window = min(
                        max(retry_after or 0.0, policy.cooldown_base_seconds)
                        * (2 ** (health.cooldown_trips - 1)),
                        policy.cooldown_max_seconds,
                    )
                    health.cooldown_until = clock() + window
                    health.consecutive_429 = 0
                    logger.warning(
                        "llm_provider_cooldown provider=%s seconds=%.0f trips=%d",
                        provider,
                        window,
                        health.cooldown_trips,
                    )
                    raise make_limited(f"{provider} is rate limited; retry later", window) from exc
            else:
                health.consecutive_429 = 0

            if attempt_no >= policy.max_retries:
                break
            backoff = rng() * min(policy.cap_seconds, policy.base_seconds * (2**attempt_no))
            wait = retry_after if retry_after is not None else backoff
            if wait > policy.max_wait_seconds:
                # The provider asked for longer than we are willing to block
                # this call: give up now, keeping the hint for the caller.
                break
            logger.warning(
                "llm_call_retry provider=%s attempt=%d/%d kind=%s wait_s=%.2f",
                provider,
                attempt_no + 1,
                policy.max_retries + 1,
                kind,
                wait,
            )
            await sleep(wait)
            continue
        health.successes += 1
        health.consecutive_429 = 0
        health.cooldown_trips = 0
        return result

    assert last is not None
    raise last
