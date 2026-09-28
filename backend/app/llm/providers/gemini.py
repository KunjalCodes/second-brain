"""Gemini implementation of `LLMClient`.

This is the **only** module in the application allowed to import `google.genai`.
Nothing outside `app/llm/` may reference Gemini, a model string, or a
provider-specific response shape -- the rest of the system depends on
`app.llm.base.LLMClient` (docs/adr/0007).

Retry scope, deliberately narrow: this adapter retries only transient
network/rate-limit/server failures, immediately, with backoff, bounded by
`LLM_MAX_RETRIES`. It does **not** retry a schema-invalid response -- resending an
identical prompt would most likely reproduce the same malformed output and just
burn tokens. Repairing a malformed response (a *different* prompt) and rescheduling
a job across a longer time horizon are both runner-level concerns (Step 10/11), not
this adapter's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from app.core.errors import (
    ConfigurationError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.llm.base import LLMResult
from app.llm.providers._gemini_errors import is_transport_error, retry_after_seconds
from app.llm.resilience import RetryPolicy, call_with_resilience

if TYPE_CHECKING:
    from google.genai import types as genai_types

logger = logging.getLogger(__name__)

#: Exponential backoff base, in seconds, between immediate in-adapter retries.

#: JSON Schema keywords Pydantic emits that Gemini's `response_schema` --
#: a restricted OpenAPI subset, not full JSON Schema -- has no field for.
#: Found empirically running Phase 5's real-LLM evaluation: the real API
#: rejects the whole request with "Unknown name 'additional_properties' ...
#: Cannot find field" if this key is present ANYWHERE in the schema tree,
#: for ANY value (`true`, `false`, or a nested schema) -- not just the "open
#: dict" case the `google-genai` SDK itself warns about client-side.
#: `additionalProperties: false` is exactly what Pydantic emits for every
#: `extra="forbid"` model in this codebase (the untrusted-LLM-output
#: validation strictness every model here needs), so this cannot be fixed by
#: relaxing model config -- only by not sending the key to Gemini. Stripping
#: it here changes nothing about how strictly a response is validated: that
#: still happens in `complete_structured` via `schema.model_validate_json`,
#: which enforces `extra="forbid"` exactly as declared regardless of what
#: schema Gemini itself was told.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({"additionalProperties"})

#: Validation-only bounds, also stripped before sending. Found running the app
#: against real Gemini: with them, the extraction schema (`ExtractionResult`)
#: is rejected with HTTP 400 "The specified schema produces a constraint that
#: has too many states for serving" -- every extraction job then failed as a
#: configuration error. Gemini's constrained decoding compiles each length /
#: count / range / pattern bound into its grammar; Pydantic still enforces all
#: of them on the response in `complete_structured`, so nothing is weakened.
#: `enum` and `type` are kept: they shape the output and are cheap.
_VALIDATION_ONLY_SCHEMA_KEYS = frozenset(
    {
        "maxLength",
        "minLength",
        "maxItems",
        "minItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
    }
)
_STRIPPED_SCHEMA_KEYS = _UNSUPPORTED_SCHEMA_KEYS | _VALIDATION_ONLY_SCHEMA_KEYS


def _to_gemini_schema(schema_cls: type[BaseModel]) -> dict:
    """Convert a Pydantic model to a dict Gemini's `response_schema` accepts.

    Two real, empirically-found incompatibilities between Pydantic's JSON
    Schema output and Gemini's Developer API structured-output mode:

    1. `additionalProperties` is unsupported in any form (see
       `_UNSUPPORTED_SCHEMA_KEYS`) -- stripped recursively, together with the
       validation-only bounds that make a large schema too complex to serve
       (`_VALIDATION_ONLY_SCHEMA_KEYS`).
    2. `$ref`/`$defs` round-tripped fine through the `google-genai` SDK's own
       Pydantic-class conversion in testing, but this function builds the
       dict by hand from `model_json_schema()` instead of handing Gemini the
       class directly (needed to reach and strip #1), so `$ref`s are
       resolved into fully inlined copies here rather than assumed to be
       handled downstream.

    A `discriminator`-tagged union (`Field(discriminator=...)`) is a related,
    separate incompatibility (Gemini's schema has no field for
    `discriminator`/`oneOf` either) -- fixed upstream by not declaring one on
    `AgentStep.step` in the first place (see that field's docstring); nothing
    here needs to handle it specially since a plain union already renders as
    `anyOf`, which Gemini does support.
    """
    raw = schema_cls.model_json_schema()
    defs = raw.pop("$defs", {})

    def _inline(node: object) -> object:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].rsplit("/", 1)[-1]
                return _inline(defs[ref_name])
            return {
                key: _inline(value)
                for key, value in node.items()
                if key not in _STRIPPED_SCHEMA_KEYS
            }
        if isinstance(node, list):
            return [_inline(item) for item in node]
        return node

    return _inline(raw)  # type: ignore[return-value]


class GeminiClient:
    """`LLMClient` backed by the Gemini API.

    The model identifier is passed in from configuration (`LLM_MODEL`) and never
    hardcoded here, so pinning or changing the exact model is a config change, not a
    code change.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        prompt_version: str,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is not set. Required when LLM_PROVIDER=gemini."
            )
        if not model:
            raise ConfigurationError("LLM_MODEL is not set. Required when LLM_PROVIDER=gemini.")
        # Imported lazily so that importing this module (e.g. for type-checking, or
        # when LLM_PROVIDER != "gemini") never requires the SDK to be configured.
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._prompt_version = prompt_version
        self._max_retries = max_retries

    async def complete_structured[T: BaseModel](
        self,
        *,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult[T]:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_to_gemini_schema(schema),
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        started = time.monotonic()
        response = await self._call_with_retries(prompt=prompt, config=config, timeout=timeout)
        latency_ms = int((time.monotonic() - started) * 1000)

        raw = response.text or ""
        # The SDK can auto-parse into `response.parsed`, but that value is never
        # trusted directly: every response is validated through the same Pydantic
        # path a fake client's output would go through, so provider trust never
        # substitutes for our own validation (docs/adr/0007).
        try:
            value = schema.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMResponseInvalidError(
                "Gemini response did not match the expected schema",
                details={"schema": schema.__name__, "errors": exc.errors()},
            ) from exc

        usage = response.usage_metadata
        return LLMResult(
            value=value,
            provider="gemini",
            model=self._model,
            prompt_version=self._prompt_version,
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
            latency_ms=latency_ms,
            raw=raw,
        )

    async def _call_with_retries(
        self,
        *,
        prompt: str,
        config: genai_types.GenerateContentConfig,
        timeout: float | None,
    ) -> genai_types.GenerateContentResponse:
        async def attempt() -> genai_types.GenerateContentResponse:
            try:
                coro = self._client.aio.models.generate_content(
                    model=self._model, contents=prompt, config=config
                )
                if timeout is not None:
                    return await asyncio.wait_for(coro, timeout=timeout)
                return await coro
            except TimeoutError as exc:
                raise LLMUnavailableError(f"Gemini did not respond within {timeout}s") from exc
            except Exception as exc:  # noqa: BLE001 - mapped into the taxonomy below
                raise _map_gemini_error(exc) from exc

        return await call_with_resilience(
            "gemini", attempt, policy=RetryPolicy(max_retries=self._max_retries)
        )


def _map_gemini_error(exc: Exception) -> Exception:
    """Translate one Gemini SDK/transport failure into the application's error
    taxonomy. Messages are safe to show a user; details stay in the chained
    exception and the log."""
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code = exc.code
        if code in (401, 403):
            return ConfigurationError(
                "Gemini rejected the configured credentials. Check GEMINI_API_KEY."
            )
        if code == 429:
            return LLMRateLimitedError(
                "Gemini is rate limiting requests (HTTP 429)",
                retry_after_seconds=retry_after_seconds(exc),
            )
        if code >= 500:
            return LLMUnavailableError(f"Gemini is unavailable (HTTP {code})")
        # Any other 4xx is a request-shape problem a retry cannot fix.
        return ConfigurationError(f"Gemini rejected the request (HTTP {code})")
    if is_transport_error(exc):
        return LLMUnavailableError("Gemini could not be reached")
    return LLMUnavailableError(f"Gemini call failed ({type(exc).__name__})")
