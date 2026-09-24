"""Embedding provider selection from configuration.

The only place `EMBEDDING_PROVIDER` is read to decide *which class* to
instantiate. Everywhere else in the application depends on `EmbeddingClient`
(ADR 0009) -- mirrors `app.llm.factory` exactly.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.embeddings.base import EmbeddingClient
from app.embeddings.fake import FakeEmbeddingClient


def build_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    settings = settings or get_settings()

    if settings.embedding_provider == "fake":
        # A first-class configuration: the whole system, including semantic
        # duplicate detection and vector retrieval, runs end to end with no
        # provider account and no network access. `embedding_model_resolved` is
        # what makes this agree with the backfill sweep's "already embedded under
        # the current model" check even when EMBEDDING_MODEL is left unset.
        return FakeEmbeddingClient(
            model=settings.embedding_model_resolved, dimension=settings.embedding_dimension
        )

    if settings.embedding_provider == "gemini":
        from app.embeddings.providers.gemini import GeminiEmbeddingClient

        return GeminiEmbeddingClient(
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            max_retries=settings.embedding_max_retries,
        )

    raise ConfigurationError(
        f"No EmbeddingClient implementation is registered for EMBEDDING_PROVIDER="
        f"{settings.embedding_provider!r} yet"
    )


_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    """Process-wide client, mirroring `get_llm_client` / `get_graph_client`."""
    global _client
    if _client is None:
        _client = build_embedding_client()
    return _client


def reset_embedding_client() -> None:
    """Drop the process-wide client. Used by tests."""
    global _client
    _client = None
