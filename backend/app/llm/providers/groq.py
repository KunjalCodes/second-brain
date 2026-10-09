"""Groq implementation of `LLMClient` (OpenAI-compatible chat completions over HTTP).

Like `gemini.py`, this is the only module that knows Groq's request/response shape.
It uses `httpx` (already a dependency) instead of an SDK. Structured output is
requested with JSON mode plus the schema in the prompt; the response is then
validated by Pydantic exactly like every other provider, so provider trust never
replaces our own validation (docs/adr/0007).

One attempt's failure is mapped into the shared error taxonomy; whether to retry
lives in `app/llm/resilience.py`.
"""

from __future__ import annotations

import contextlib
import json
import time

import httpx
from pydantic import BaseModel, ValidationError

from app.core.errors import (
    ConfigurationError,
    LLMRateLimitedError,
    LLMResponseInvalidError,
    LLMUnavailableError,
)
from app.llm.base import LLMResult
from app.llm.resilience import RetryPolicy, call_with_resilience

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_TIMEOUT = 30.0


class GroqClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        prompt_version: str,
        max_retries: int = 3,
        base_url: str = GROQ_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError("GROQ_API_KEY is not set. Required when using Groq.")
        if not model:
            raise ConfigurationError(
                "The Groq model name is not set (LLM_MODEL or LLM_FALLBACK_MODEL)."
            )
        self._model = model
        self._prompt_version = prompt_version
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            follow_redirects=False,  # never forward the key to another host
        )

    async def complete_structured[T: BaseModel](
        self,
        *,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult[T]:
        instruction = (
            f"{prompt}\n\nRespond with ONLY a single JSON object that satisfies this JSON Schema. "
            f"No prose, no markdown.\n{json.dumps(schema.model_json_schema())}"
        )
        body: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": instruction}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        started = time.monotonic()

        async def attempt() -> dict:
            try:
                resp = await self._http.post(
                    "/chat/completions", json=body, timeout=timeout or _DEFAULT_TIMEOUT
                )
            except httpx.TimeoutException as exc:
                raise LLMUnavailableError(
                    f"Groq did not respond within {timeout or _DEFAULT_TIMEOUT}s"
                ) from exc
            except httpx.TransportError as exc:
                raise LLMUnavailableError("Groq could not be reached") from exc
            if resp.status_code >= 400:
                raise _map_status(resp)
            try:
                return resp.json()
            except ValueError as exc:
                raise LLMUnavailableError("Groq returned a non-JSON response") from exc

        data = await call_with_resilience(
            "groq", attempt, policy=RetryPolicy(max_retries=self._max_retries)
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            raw = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseInvalidError("Groq response had no message content") from exc
        try:
            value = schema.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMResponseInvalidError(
                "Groq response did not match the expected schema",
                details={"schema": schema.__name__, "errors": exc.errors()},
            ) from exc
        usage = data.get("usage") or {}
        return LLMResult(
            value=value,
            provider="groq",
            model=self._model,
            prompt_version=self._prompt_version,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            raw=raw,
        )


def _map_status(resp: httpx.Response) -> Exception:
    """One failed HTTP response -> the application's error taxonomy (messages are safe to show)."""
    code = resp.status_code
    if code in (401, 403):
        return ConfigurationError("Groq rejected the configured credentials. Check GROQ_API_KEY.")
    if code == 429:
        after: float | None = None
        with contextlib.suppress(ValueError):
            after = float(resp.headers.get("retry-after", ""))
        return LLMRateLimitedError(
            "Groq is rate limiting requests (HTTP 429)", retry_after_seconds=after
        )
    if code >= 500:
        return LLMUnavailableError(f"Groq is unavailable (HTTP {code})")
    try:
        error_code = (resp.json().get("error") or {}).get("code")
    except ValueError:
        error_code = None
    if error_code == "json_validate_failed":
        return LLMResponseInvalidError("Groq could not produce valid JSON for this request")
    return ConfigurationError(f"Groq rejected the request (HTTP {code})")
