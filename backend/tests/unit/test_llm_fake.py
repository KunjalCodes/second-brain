"""FakeLLMClient: the deterministic LLMClient the whole test suite depends on.

If this file needs an API key or network access, something has gone wrong with the
entire test strategy -- it must not.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.errors import LLMResponseInvalidError, LLMUnavailableError
from app.llm.fake import FakeLLMClient


class Schema(BaseModel):
    value: int


class TestSingleResponse:
    async def test_returns_the_configured_response_every_call(self) -> None:
        fake = FakeLLMClient(response=Schema(value=5))
        first = await fake.complete_structured(prompt="a", schema=Schema)
        second = await fake.complete_structured(prompt="b", schema=Schema)
        assert first.value.value == second.value.value == 5

    async def test_result_carries_provider_metadata(self) -> None:
        fake = FakeLLMClient(
            response=Schema(value=1),
            provider_name="fake",
            model_name="fake-v1",
            prompt_version="v1",
        )
        result = await fake.complete_structured(prompt="x", schema=Schema)
        assert result.provider == "fake"
        assert result.model == "fake-v1"
        assert result.prompt_version == "v1"
        assert result.raw  # the raw JSON is retained for replay/debugging

    async def test_calls_are_recorded(self) -> None:
        fake = FakeLLMClient(response=Schema(value=1))
        await fake.complete_structured(prompt="hello", schema=Schema, temperature=0.2)
        assert fake.call_count == 1
        assert fake.calls[0]["prompt"] == "hello"
        assert fake.calls[0]["temperature"] == 0.2


class TestSequentialResponses:
    async def test_responses_are_consumed_in_order(self) -> None:
        fake = FakeLLMClient(responses=[Schema(value=1), Schema(value=2)])
        first = await fake.complete_structured(prompt="a", schema=Schema)
        second = await fake.complete_structured(prompt="b", schema=Schema)
        assert (first.value.value, second.value.value) == (1, 2)

    async def test_exhausting_the_queue_raises_a_clear_error(self) -> None:
        fake = FakeLLMClient(responses=[Schema(value=1)])
        await fake.complete_structured(prompt="a", schema=Schema)
        with pytest.raises(RuntimeError, match="exhausted"):
            await fake.complete_structured(prompt="b", schema=Schema)


class TestFailureInjection:
    async def test_configured_failure_is_raised(self) -> None:
        fake = FakeLLMClient(failure=LLMUnavailableError("simulated outage"))
        with pytest.raises(LLMUnavailableError):
            await fake.complete_structured(prompt="x", schema=Schema)

    async def test_call_is_still_recorded_before_raising(self) -> None:
        fake = FakeLLMClient(failure=LLMUnavailableError("boom"))
        with pytest.raises(LLMUnavailableError):
            await fake.complete_structured(prompt="x", schema=Schema)
        assert fake.call_count == 1


class TestMalformedOutput:
    async def test_raw_override_that_fails_schema_validation_raises_response_invalid(
        self,
    ) -> None:
        fake = FakeLLMClient(raw_override='{"value": "not an int"}')
        with pytest.raises(LLMResponseInvalidError):
            await fake.complete_structured(prompt="x", schema=Schema)

    async def test_non_json_raw_override_raises_response_invalid(self) -> None:
        fake = FakeLLMClient(raw_override="this is not json at all")
        with pytest.raises(LLMResponseInvalidError):
            await fake.complete_structured(prompt="x", schema=Schema)

    async def test_missing_required_field_raises_response_invalid(self) -> None:
        fake = FakeLLMClient(raw_override="{}")
        with pytest.raises(LLMResponseInvalidError):
            await fake.complete_structured(prompt="x", schema=Schema)


class TestRespondFn:
    async def test_response_can_depend_on_the_prompt(self) -> None:
        def respond(prompt: str) -> Schema:
            return Schema(value=len(prompt))

        fake = FakeLLMClient(respond_fn=respond)
        result = await fake.complete_structured(prompt="hello", schema=Schema)
        assert result.value.value == 5

    async def test_respond_fn_can_return_a_failure(self) -> None:
        def respond(prompt: str) -> Exception:
            return LLMUnavailableError("dynamic failure")

        fake = FakeLLMClient(respond_fn=respond)
        with pytest.raises(LLMUnavailableError):
            await fake.complete_structured(prompt="x", schema=Schema)

    async def test_respond_fn_takes_priority_over_response(self) -> None:
        fake = FakeLLMClient(response=Schema(value=1), respond_fn=lambda p: Schema(value=99))
        result = await fake.complete_structured(prompt="x", schema=Schema)
        assert result.value.value == 99


class TestMisconfiguration:
    async def test_no_response_configured_raises_a_clear_error(self) -> None:
        fake = FakeLLMClient()
        with pytest.raises(RuntimeError, match="no response"):
            await fake.complete_structured(prompt="x", schema=Schema)


class TestLoadFixture:
    """Recorded fixtures in tests/fixtures/llm/, an alternative to constructing
    CandidateMemory objects inline for tests that reuse the same recording.
    """

    async def test_loads_and_validates_a_recorded_extraction_result(self) -> None:
        from app.llm.fake import load_fixture
        from app.models.extraction import ExtractionResult

        result = load_fixture("new_technology", ExtractionResult)
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.persistence == "persistent"
        assert {e.name for e in candidate.entities} == {"Drawing Extractor", "Gemini"}
        # sanity: usable directly as a FakeLLMClient response
        fake = FakeLLMClient(response=result)
        loaded = await fake.complete_structured(prompt="x", schema=ExtractionResult)
        assert loaded.value.candidates[0].relationships[0].predicate == "uses"

    async def test_loads_an_empty_candidates_fixture(self) -> None:
        from app.llm.fake import load_fixture
        from app.models.extraction import ExtractionResult

        result = load_fixture("empty_candidates", ExtractionResult)
        assert result.candidates == []

    async def test_loads_a_temporary_persistence_fixture(self) -> None:
        from app.llm.fake import load_fixture
        from app.models.extraction import ExtractionResult

        result = load_fixture("temporary_statement", ExtractionResult)
        assert result.candidates[0].persistence == "temporary"

    async def test_malformed_fixture_fails_validation_at_load_time(self) -> None:
        from pydantic import ValidationError

        from app.llm.fake import load_fixture
        from app.models.extraction import ExtractionResult

        with pytest.raises(ValidationError):
            load_fixture("malformed_confidence_out_of_range", ExtractionResult)

    async def test_missing_fixture_raises_a_clear_error(self) -> None:
        from app.llm.fake import load_fixture
        from app.models.extraction import ExtractionResult

        with pytest.raises(FileNotFoundError, match="no_such_fixture"):
            load_fixture("no_such_fixture", ExtractionResult)
