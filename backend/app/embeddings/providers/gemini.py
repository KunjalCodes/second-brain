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

from app.core.errors import ConfigurationError, EmbeddingUnavailableError
from app.embeddings.base import EmbeddingResult

if TYPE_CHECKING:
    from google.genai import types as genai_types

logger = logging.getLogger(__name__)

#: Exponential backoff base, in seconds, between immediate in-adapter retries.
_RETRY_BACKOFF_BASE_SECONDS = 1.0


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
        from google.genai import errors

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                coro = self._client.aio.models.embed_content(
                    model=self._model, contents=text, config=config
                )
                if timeout is not None:
                    return await asyncio.wait_for(coro, timeout=timeout)
                return await coro
            except TimeoutError as exc:
                last_error = EmbeddingUnavailableError(
                    f"Gemini did not respond within {timeout}s"
                )
                last_error.__cause__ = exc
            except errors.APIError as exc:
                mapped = _map_gemini_error(exc)
                if not isinstance(mapped, EmbeddingUnavailableError):
                    # Auth/config failures are not retryable -- raise immediately.
                    raise mapped from exc
                last_error = mapped
                last_error.__cause__ = exc

            if attempt < self._max_retries:
                backoff = _RETRY_BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "Gemini embedding call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    self._max_retries + 1,
                    backoff,
                    last_error,
                )
                await asyncio.sleep(backoff)

        assert last_error is not None  # the loop always sets it before exiting
        raise last_error


def _map_gemini_error(exc: Exception) -> Exception:
    """Translate a Gemini SDK exception into the application's error taxonomy."""
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code = exc.code
        if code in (401, 403):
            return ConfigurationError(
                "Gemini rejected the configured credentials. Check GEMINI_API_KEY."
            )
        if code == 429 or code >= 500:
            return EmbeddingUnavailableError(f"Gemini is unavailable (HTTP {code})")
        return ConfigurationError(f"Gemini rejected the embedding request (HTTP {code})")
    return EmbeddingUnavailableError(f"Gemini embedding call failed: {exc}")
