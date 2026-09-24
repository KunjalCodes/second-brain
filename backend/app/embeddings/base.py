"""Embedding provider abstraction (ADR 0009, PHASE3_PLAN.md section 1).

Structurally identical in shape to `app.llm.base.LLMClient`/`LLMResult`: the rest
of the application depends on `EmbeddingClient`, never on a concrete provider.
Only `app/embeddings/providers/gemini.py` (and any future provider adapter) may
import an embedding SDK; everything else -- the embedding pipeline, semantic
similarity, vector retrieval -- sees this protocol and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class EmbeddingClient(Protocol):
    """Structural interface every embedding provider adapter implements."""

    async def embed(self, *, text: str, timeout: float | None = None) -> EmbeddingResult:
        """Embed one piece of text.

        Raises `EmbeddingUnavailableError` for a transient failure (timeout, rate
        limit, 5xx) worth retrying, and `ConfigurationError` for an unrecoverable
        setup problem (bad credentials, unknown model) that a retry cannot fix --
        mirroring `LLMClient.complete_structured`'s exact error taxonomy split.
        """
        ...


@dataclass(frozen=True)
class EmbeddingResult:
    """A vector plus the reproducibility metadata EVALUATION.md requires.

    Captured once, at the boundary, the same way `LLMResult` captures
    provider/model/prompt_version per extraction call -- `provider`/`model` travel
    with every `memory_embeddings` row (PHASE3_PLAN.md section 12), so a stored
    vector's provenance is never ambiguous.
    """

    vector: list[float]
    provider: str
    model: str
    dimension: int
    latency_ms: int
