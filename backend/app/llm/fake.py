"""Deterministic `LLMClient` for tests.

**Tests must never require an API key, network access, or money.** `FakeLLMClient`
is the default provider (`LLM_PROVIDER=fake`) and the only one the normal test suite
uses; the real Gemini adapter is exercised solely by the opt-in `-m llm` suite.

Every response still passes through the same Pydantic validation path a real
provider's output would, so a fixture that doesn't satisfy the schema fails exactly
the way a real malformed response would (see `LLMResponseInvalidError`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import LLMResponseInvalidError
from app.llm.base import LLMResult

#: What a fixture, or `respond_fn`, may return.
FakeResponse = BaseModel | str | Exception

RespondFn = Callable[[str], FakeResponse]


class FakeLLMClient:
    """A drop-in `LLMClient` with a fully scripted response.

    Exactly one construction style is expected per instance -- tests read most
    clearly when each fake is built for one scenario:

        FakeLLMClient(response=some_extraction_result)         # every call, same answer
        FakeLLMClient(responses=[first, second])                # consumed in call order
        FakeLLMClient(raw_override='{"candidates": "not a list"}')  # malformed shape
        FakeLLMClient(failure=LLMUnavailableError("boom"))       # exercise retry paths
        FakeLLMClient(respond_fn=lambda prompt: ... )            # response depends on input

    `respond_fn` takes priority if given; otherwise `responses` is consumed in
    order; otherwise `response`/`raw_override`/`failure` apply to every call.
    """

    def __init__(
        self,
        *,
        response: BaseModel | None = None,
        responses: list[BaseModel] | None = None,
        raw_override: str | None = None,
        failure: Exception | None = None,
        respond_fn: RespondFn | None = None,
        provider_name: str = "fake",
        model_name: str = "fake-v1",
        prompt_version: str = "test",
    ) -> None:
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self._raw_override = raw_override
        self._failure = failure
        self._respond_fn = respond_fn
        self._provider_name = provider_name
        self._model_name = model_name
        self._prompt_version = prompt_version

        #: Every call, recorded for assertions -- e.g. "the gate should have
        #: suppressed this, so the fake must never have been called".
        self.calls: list[dict[str, Any]] = []

    async def complete_structured[T: BaseModel](
        self,
        *,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult[T]:
        self.calls.append(
            {"prompt": prompt, "schema": schema, "temperature": temperature}
        )

        outcome = self._next_outcome(prompt)
        if isinstance(outcome, Exception):
            raise outcome

        raw = outcome if isinstance(outcome, str) else outcome.model_dump_json()

        try:
            value = schema.model_validate_json(raw)
        except ValidationError as exc:
            raise LLMResponseInvalidError(
                "Fake response did not match the expected schema",
                details={"schema": schema.__name__, "errors": exc.errors()},
            ) from exc

        return LLMResult(
            value=value,
            provider=self._provider_name,
            model=self._model_name,
            prompt_version=self._prompt_version,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            raw=raw,
        )

    def _next_outcome(self, prompt: str) -> FakeResponse:
        if self._respond_fn is not None:
            return self._respond_fn(prompt)
        if self._failure is not None:
            return self._failure
        if self._raw_override is not None:
            return self._raw_override
        if self._responses is not None:
            if not self._responses:
                raise RuntimeError(
                    "FakeLLMClient.responses exhausted -- the code under test called "
                    "complete_structured more times than the test scripted for"
                )
            return self._responses.pop(0)
        if self._response is not None:
            return self._response
        raise RuntimeError(
            "FakeLLMClient was constructed with no response, responses, "
            "raw_override, failure, or respond_fn"
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


def load_fixture[T: BaseModel](name: str, schema: type[T]) -> T:
    """Load and validate a recorded extraction fixture from tests/fixtures/llm/.

    Centralised so every fixture-based test loads fixtures the same way, and so a
    fixture that doesn't satisfy the current schema fails loudly at load time rather
    than producing a confusing failure deeper in a test.
    """
    fixtures_dir = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm"
    path = fixtures_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No LLM fixture named {name!r} at {path}")
    data = json.loads(path.read_text())
    return schema.model_validate(data)
