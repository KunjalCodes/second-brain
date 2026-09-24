"""LLM provider abstraction (docs/adr/0007).

The rest of the application depends on `LLMClient` -- never on a concrete provider.
Only `app/llm/providers/gemini.py` (and future `openai.py`/`anthropic.py`) may import
a provider SDK; everything else, including the extraction pipeline, sees this
protocol and nothing else.

`complete_structured` always returns an **already-validated** Pydantic model.
Callers never see raw provider JSON, and validation happens in exactly one place
regardless of which provider produced the response -- matching how
`app.graph.connection` is the only module that sees the Neo4j driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class LLMClient(Protocol):
    """Structural interface every provider adapter implements."""

    async def complete_structured[T: BaseModel](
        self,
        *,
        prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult[T]:
        """Ask the model to produce output matching `schema`.

        Raises `LLMUnavailableError` for a transient failure (timeout, rate limit,
        5xx) that is worth retrying, `LLMResponseInvalidError` when the provider
        replied but the response does not satisfy `schema`, and
        `ConfigurationError` for an unrecoverable setup problem (bad credentials,
        unknown model) that a retry cannot fix.
        """
        ...


@dataclass(frozen=True)
class LLMResult[T]:
    """A validated result plus the reproducibility metadata EVALUATION.md requires.

    Captured at the boundary, once, rather than retrofitted per call site: model,
    provider and prompt_version travel with every extraction job
    (`app/db/models.py::ExtractionJob`), and `raw` is retained so a bad extraction
    can be replayed and debugged without calling the model again.
    """

    value: T
    provider: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw: str
