"""GeminiEmbeddingClient: construction guards and error mapping. No network access,
mirroring test_llm_gemini.py exactly.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConfigurationError, EmbeddingUnavailableError
from app.embeddings.providers.gemini import GeminiEmbeddingClient, _map_gemini_error


class TestConstructionGuards:
    def test_missing_api_key_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
            GeminiEmbeddingClient(api_key="", model="embedding-x", dimension=768)

    def test_missing_model_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="EMBEDDING_MODEL"):
            GeminiEmbeddingClient(api_key="key", model="", dimension=768)

    def test_valid_construction_succeeds_without_any_network_call(self) -> None:
        client = GeminiEmbeddingClient(
            api_key="fake-key-for-testing", model="embedding-x", dimension=768
        )
        assert client is not None


class TestErrorMapping:
    def _api_error(self, code: int):  # noqa: ANN202 - returns the SDK's own error type
        from google.genai import errors

        return errors.APIError(code, {"error": {"message": "boom"}})

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_errors_map_to_configuration_error(self, code: int) -> None:
        mapped = _map_gemini_error(self._api_error(code))
        assert isinstance(mapped, ConfigurationError)
        assert "GEMINI_API_KEY" in mapped.message

    @pytest.mark.parametrize("code", [429, 500, 502, 503])
    def test_retryable_errors_map_to_unavailable(self, code: int) -> None:
        mapped = _map_gemini_error(self._api_error(code))
        assert isinstance(mapped, EmbeddingUnavailableError)

    def test_other_client_errors_map_to_configuration_error_not_retryable(self) -> None:
        mapped = _map_gemini_error(self._api_error(400))
        assert isinstance(mapped, ConfigurationError)

    def test_unrecognised_exception_maps_to_unavailable_rather_than_crashing(self) -> None:
        mapped = _map_gemini_error(RuntimeError("some transport-level failure"))
        assert isinstance(mapped, EmbeddingUnavailableError)


class TestModelIdentityIsConfigurationOnly:
    def test_no_model_string_appears_in_the_provider_module_source(self) -> None:
        """The exact embedding model id must come only from EMBEDDING_MODEL --
        pinning it in code would defeat the point of making it configurable
        (ADR 0009, mirroring docs/adr/0007's rule for the LLM provider).
        """
        import inspect

        import app.embeddings.providers.gemini as module

        source = inspect.getsource(module)
        assert "embedding-001" not in source
        assert "text-embedding" not in source


class TestGeminiImportIsolation:
    def test_google_genai_is_imported_only_by_the_two_known_provider_files(self) -> None:
        """ADR 0009 / docs/adr/0007: `google.genai` must be isolated to exactly
        `app/llm/providers/gemini.py` (the LLM adapter) and
        `app/embeddings/providers/gemini.py` (the embedding adapter) -- nowhere
        else in the application.
        """
        import pathlib
        import re

        app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
        pattern = re.compile(
            r"\bimport\s+google\.genai\b|\bfrom\s+google\.genai\b|\bfrom\s+google\s+import\s+genai\b"
        )
        allowed = {
            app_dir / "llm" / "providers" / "gemini.py",
            app_dir / "embeddings" / "providers" / "gemini.py",
        }
        offenders = [
            path
            for path in app_dir.rglob("*.py")
            if path not in allowed and pattern.search(path.read_text())
        ]
        assert offenders == []
