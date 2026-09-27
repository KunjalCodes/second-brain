"""Provider selection from configuration.

The only place `LLM_PROVIDER` is read to decide *which class* to instantiate.
Everywhere else in the application depends on `LLMClient` (docs/adr/0007).
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.llm.base import LLMClient
from app.llm.fake import OfflineLLMClient


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()

    if settings.llm_provider == "fake":
        # A first-class configuration: the whole system runs end to end with no
        # provider account at all. In a running app that means the offline
        # client (honest, deterministic answers -- Phase 11, D7); tests script
        # `FakeLLMClient` directly.
        return OfflineLLMClient()

    if settings.llm_provider == "gemini":
        from app.llm.providers.gemini import GeminiClient

        return GeminiClient(
            api_key=settings.gemini_api_key,
            model=settings.llm_model,
            prompt_version=settings.extraction_prompt_version,
            max_retries=settings.llm_max_retries,
        )

    # "openai" and "anthropic" are accepted by Settings validation (so config can
    # name them ahead of the adapter existing) but have no adapter yet.
    raise ConfigurationError(
        f"No LLMClient implementation is registered for LLM_PROVIDER="
        f"{settings.llm_provider!r} yet"
    )


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Process-wide client, mirroring `get_graph_client` / `get_database`."""
    global _client
    if _client is None:
        _client = build_llm_client()
    return _client


def reset_llm_client() -> None:
    """Drop the process-wide client. Used by tests."""
    global _client
    _client = None
