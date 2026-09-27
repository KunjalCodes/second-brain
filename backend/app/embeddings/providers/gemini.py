"""Gemini implementation of `EmbeddingClient`.

This is the **only** module in the application allowed to import `google.genai`
for embeddings -- mirrors `app.llm.providers.gemini` exactly: nothing outside
`app/embeddings/` may reference Gemini, a model string, or a provider-specific
response shape.

Retry scope, deliberately narrow, same rationale as the LLM adapter: only
transient network/rate-limit/server failures are retried, immediately, with
backoff, bounded by `EMBEDDING_MAX_RETRIES`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from app.core.errors import (
    ConfigurationError,
    EmbeddingRateLimitedError,
    EmbeddingUnavailableError,
)
from app.embeddings.base import EmbeddingResult
from app.llm.providers._gemini_errors import is_transport_error, retry_after_seconds
from app.llm.resilience import RetryPolicy, call_with_resilience

if TYPE_CHECKING:
    from google.genai import types as genai_types

logger = logging.getLogger(__name__)

#: Exponential backoff base, in seconds, between immediate in-adapter retries.


class GeminiEmbeddingClient:
    """`EmbeddingClient` backed by the Gemini API.

    The model identifier and output dimension are passed in from configuration
    (`EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`) and never hardcoded here.
    `output_dimensionality` is requested explicitly on every call rather than
    trusting the model's default, which is what makes the dimension a property
    this adapter controls and can be validated against the pgvector column
    (`Database.connect`, PHASE3_PLAN.md section 12) rather than something that
    could silently vary between calls.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimension: int,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is not set. Required when EMBEDDING_PROVIDER=gemini."
            )
        if not model:
            raise ConfigurationError(
                "EMBEDDING_MODEL is not set. Required when EMBEDDING_PROVIDER=gemini."
            )
        # Imported lazily, same rationale as app.llm.providers.gemini: importing
        # this module must never require the SDK to be configured when
        # EMBEDDING_PROVIDER != "gemini".
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._dimension = dimension
        self._max_retries = max_retries

    async def embed(self, *, text: str, timeout: float | None = None) -> EmbeddingResult:
        from google.genai import types

        config = types.EmbedContentConfig(output_dimensionality=self._dimension)

        started = time.monotonic()
        response = await self._call_with_retries(text=text, config=config, timeout=timeout)
        latency_ms = int((time.monotonic() - started) * 1000)

        if not response.embeddings:
            raise EmbeddingUnavailableError("Gemini returned no embedding for the input text")
        values = response.embeddings[0].values
        if values is None or len(values) != self._dimension:
            raise EmbeddingUnavailableError(
                f"Gemini returned a vector of unexpected shape "
                f"(expected dimension {self._dimension})"
            )

        return EmbeddingResult(
            vector=list(values),
            provider="gemini",
            model=self._model,
            dimension=self._dimension,
            latency_ms=latency_ms,
        )

    async def _call_with_retries(
        self,
        *,
        text: str,
        config: genai_types.EmbedContentConfig,
        timeout: float | None,
    ) -> genai_types.EmbedContentResponse:
        async def attempt() -> genai_types.EmbedContentResponse:
            try:
                coro = self._client.aio.models.embed_content(
                    model=self._model, contents=text, config=config
                )
                if timeout is not None:
                    return await asyncio.wait_for(coro, timeout=timeout)
                return await coro
            except TimeoutError as exc:
                raise EmbeddingUnavailableError(
                    f"Gemini did not respond within {timeout}s"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - mapped into the taxonomy below
                raise _map_gemini_error(exc) from exc

        return await call_with_resilience(
            "gemini-embeddings",
            attempt,
            policy=RetryPolicy(max_retries=self._max_retries),
            rate_limited_error=lambda msg, after: EmbeddingRateLimitedError(
                msg, retry_after_seconds=after
            ),
        )


def _map_gemini_error(exc: Exception) -> Exception:
    """Translate one Gemini SDK/transport failure into the application's error taxonomy."""
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code = exc.code
        if code in (401, 403):
            return ConfigurationError(
                "Gemini rejected the configured credentials. Check GEMINI_API_KEY."
            )
        if code == 429:
            return EmbeddingRateLimitedError(
                "Gemini is rate limiting embedding requests (HTTP 429)",
                retry_after_seconds=retry_after_seconds(exc),
            )
        if code >= 500:
            return EmbeddingUnavailableError(f"Gemini is unavailable (HTTP {code})")
        return ConfigurationError(f"Gemini rejected the embedding request (HTTP {code})")
    if is_transport_error(exc):
        return EmbeddingUnavailableError("Gemini could not be reached")
    return EmbeddingUnavailableError(f"Gemini embedding call failed ({type(exc).__name__})")
