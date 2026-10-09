"""Provider selection from configuration.

The only place `LLM_PROVIDER` is read to decide *which class* to instantiate.
Everywhere else in the application depends on `LLMClient` (docs/adr/0007).
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.llm.base import LLMClient
from app.llm.fake import OfflineLLMClient


def _build_one(
    provider: str, model: str, settings: Settings, *, max_retries: int
) -> LLMClient:
    if provider == "gemini":
        from app.llm.providers.gemini import GeminiClient

        return GeminiClient(
            api_key=settings.gemini_api_key,
            model=model,
            prompt_version=settings.extraction_prompt_version,
            max_retries=max_retries,
        )
    if provider == "groq":
        from app.llm.providers.groq import GroqClient

        return GroqClient(
            api_key=settings.groq_api_key,
            model=model,
            prompt_version=settings.extraction_prompt_version,
            max_retries=max_retries,
        )
    # "openai" and "anthropic" are accepted by Settings validation (so config can
    # name them ahead of the adapter existing) but have no adapter yet.
    raise ConfigurationError(
        f"No LLMClient implementation is registered for LLM_PROVIDER={provider!r} yet"
    )


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()

    if settings.llm_provider == "fake":
        # A first-class configuration: the whole system runs end to end with no
        # provider account at all. In a running app that means the offline
        # client (honest, deterministic answers -- Phase 11, D7); tests script
        # `FakeLLMClient` directly.
        return OfflineLLMClient()

    fallback_provider = settings.llm_fallback_provider
    if not fallback_provider:
        return _build_one(
            settings.llm_provider,
            settings.llm_model,
            settings,
            max_retries=settings.llm_max_retries,
        )
    # With a fallback configured the primary fails fast (one retry) so a slow or
    # throttled provider costs seconds, not minutes, before the fallback answers.
    from app.llm.fallback import FallbackLLMClient

    return FallbackLLMClient(
        _build_one(settings.llm_provider, settings.llm_model, settings, max_retries=1),
        _build_one(
            fallback_provider,
            settings.llm_fallback_model,
            settings,
            max_retries=settings.llm_max_retries,
        ),
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
