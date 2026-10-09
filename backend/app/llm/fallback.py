"""Primary-then-fallback `LLMClient` (e.g. Gemini, falling back to Groq).

Only *availability* failures trigger the fallback: a timeout / 5xx / transport error
(`LLMUnavailableError`) or throttling (`LLMRateLimitedError`, including the primary's
cooldown fail-fast). A schema-invalid reply is NOT retried on the other provider (the
extraction runner makes its own repair call), and a `ConfigurationError` is raised as-is
so a bad key is never hidden behind a working fallback.

`LLMResult.provider`/`model` report the provider that actually answered, so extraction
jobs and evaluation records stay truthful. If both fail, the primary's error is raised
(a rate limit stays a rate limit, so background jobs can wait it out).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from app.core.errors import LLMRateLimitedError, LLMUnavailableError
from app.llm.base import LLMClient, LLMResult

logger = logging.getLogger(__name__)


class FallbackLLMClient:
    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete_structured[T: BaseModel](
        self,
        *,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult[T]:
        kwargs = {
            "prompt": prompt,
            "schema": schema,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        try:
            return await self._primary.complete_structured(**kwargs)
        except (LLMUnavailableError, LLMRateLimitedError) as primary_error:
            logger.warning(
                "llm_fallback primary_error=%s", type(primary_error).__name__
            )
            try:
                return await self._fallback.complete_structured(**kwargs)
            except (LLMUnavailableError, LLMRateLimitedError):
                raise primary_error from None
