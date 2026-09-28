"""GeminiClient: construction guards and error mapping. No network access -- every
test here either fails before a real HTTP call would happen, or mocks the SDK call.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.errors import ConfigurationError, LLMUnavailableError
from app.llm.providers.gemini import GeminiClient, _map_gemini_error, _to_gemini_schema


class TestConstructionGuards:
    def test_missing_api_key_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
            GeminiClient(api_key="", model="gemini-x", prompt_version="v1")

    def test_missing_model_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="LLM_MODEL"):
            GeminiClient(api_key="key", model="", prompt_version="v1")

    def test_valid_construction_succeeds_without_any_network_call(self) -> None:
        # Construction only builds the SDK client object; it must not make a
        # network call, so this must succeed offline.
        client = GeminiClient(api_key="fake-key-for-testing", model="gemini-x", prompt_version="v1")
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
        assert isinstance(mapped, LLMUnavailableError)

    def test_other_client_errors_map_to_configuration_error_not_retryable(self) -> None:
        mapped = _map_gemini_error(self._api_error(400))
        assert isinstance(mapped, ConfigurationError)

    def test_unrecognised_exception_maps_to_unavailable_rather_than_crashing(self) -> None:
        mapped = _map_gemini_error(RuntimeError("some transport-level failure"))
        assert isinstance(mapped, LLMUnavailableError)


def _has_key(node: object, key: str) -> bool:
    if isinstance(node, dict):
        if key in node:
            return True
        return any(_has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_has_key(item, key) for item in node)
    return False


class TestToGeminiSchema:
    """Regression coverage for the real, empirically-found incompatibilities
    between Pydantic's JSON Schema output and the real Gemini Developer
    API's `response_schema` (see `_to_gemini_schema`'s own docstring for the
    full story) -- found running Phase 5's real-LLM evaluation, not a
    hypothetical concern.
    """

    def test_strips_additional_properties_from_an_extra_forbid_model(self) -> None:
        class Strict(BaseModel):
            model_config = ConfigDict(extra="forbid")

            name: str

        schema = _to_gemini_schema(Strict)
        assert not _has_key(schema, "additionalProperties")

    def test_strips_additional_properties_from_a_nested_ref(self) -> None:
        class Inner(BaseModel):
            model_config = ConfigDict(extra="forbid")

            value: int

        class Outer(BaseModel):
            model_config = ConfigDict(extra="forbid")

            inner: Inner

        schema = _to_gemini_schema(Outer)
        assert not _has_key(schema, "additionalProperties")
        # The nested model's own fields must survive inlining, not just its
        # container -- a bug here would silently produce `{}` for `inner`.
        assert schema["properties"]["inner"]["properties"]["value"]["type"] == "integer"

    def test_inlines_refs_instead_of_leaving_them_for_gemini_to_resolve(self) -> None:
        class Inner(BaseModel):
            value: int

        class Outer(BaseModel):
            inner: Inner

        schema = _to_gemini_schema(Outer)
        assert not _has_key(schema, "$ref")
        assert not _has_key(schema, "$defs")

    def test_strips_validation_only_bounds_but_keeps_enums_and_types(self) -> None:
        """Real Gemini rejected the full extraction schema with HTTP 400 ("too
        many states for serving") until these bounds were stripped; Pydantic
        still enforces them on the response."""
        from app.models.extraction import ExtractionResult

        schema = _to_gemini_schema(ExtractionResult)
        for key in (
            "maxLength", "minLength", "maxItems", "minItems", "minimum", "maximum", "pattern",
        ):  # fmt: skip
            assert not _has_key(schema, key), key
        assert _has_key(schema, "enum")
        candidate = schema["properties"]["candidates"]["items"]
        assert candidate["properties"]["confidence"]["type"] == "number"

    def test_still_produces_a_json_serializable_dict(self) -> None:
        class Simple(BaseModel):
            model_config = ConfigDict(extra="forbid")

            query: str

        schema = _to_gemini_schema(Simple)
        json.dumps(schema)  # must not raise


class TestModelIdentityIsConfigurationOnly:
    def test_no_model_string_appears_in_the_provider_module_source(self) -> None:
        """The exact Gemini model id must come only from LLM_MODEL -- pinning it in
        code would defeat the point of making it configurable (docs/adr/0007).
        """
        import inspect

        import app.llm.providers.gemini as module

        source = inspect.getsource(module)
        # A loose but meaningful check: no "gemini-" literal anywhere in the
        # adapter's own source (docstrings/comments referencing the provider name
        # "Gemini" itself are fine; a specific model string is not).
        assert "gemini-1" not in source
        assert "gemini-2" not in source
