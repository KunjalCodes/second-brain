"""The Phase 5 agent contract (docs/PHASE5_PLAN.md) -- pure schema tests, no
database, no LLM. Mirrors `tests/unit/test_extraction_schemas.py`'s style.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.agent import (
    TOOL_ARGS_SCHEMAS,
    AgentStep,
    FinalAnswerStep,
    GetEntityArgs,
    ProposeMemoryUpdateArgs,
    SearchMemoryArgs,
    ToolCallStep,
    ToolName,
)
from app.models.extraction import CandidateEntity, CandidateRelationship


class TestNoUserIdField:
    """The single load-bearing security property of the whole tool layer:
    no schema an LLM's structured output populates may carry a user_id.
    """

    def test_no_tool_args_schema_has_a_user_id_field(self) -> None:
        for tool_name, schema in TOOL_ARGS_SCHEMAS.items():
            assert "user_id" not in schema.model_fields, tool_name

    def test_agent_step_and_its_members_have_no_user_id_field(self) -> None:
        for schema in (AgentStep, ToolCallStep, FinalAnswerStep):
            assert "user_id" not in schema.model_fields, schema.__name__


class TestToolNameCoverage:
    def test_every_tool_name_has_an_args_schema(self) -> None:
        """`ToolName`'s Literal values and `TOOL_ARGS_SCHEMAS`'s keys must
        never drift apart -- a new tool added to one and not the other would
        silently fail dispatch or validation at runtime instead of at
        import/test time.
        """
        tool_names = set(ToolName.__args__)  # type: ignore[attr-defined]
        assert set(TOOL_ARGS_SCHEMAS.keys()) == tool_names


class TestExtraFieldsForbidden:
    def test_search_memory_args_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            SearchMemoryArgs.model_validate({"query": "x", "user_id": "sneaky"})

    def test_get_entity_args_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            GetEntityArgs.model_validate({"name_or_id": "x", "extra": "field"})


class TestSearchMemoryArgs:
    def test_as_of_scope_requires_a_date(self) -> None:
        with pytest.raises(ValidationError):
            SearchMemoryArgs(query="x", temporal_scope="as_of")

    def test_as_of_scope_with_date_is_valid(self) -> None:
        from datetime import UTC, datetime

        args = SearchMemoryArgs(query="x", temporal_scope="as_of", as_of=datetime.now(UTC))
        assert args.as_of is not None

    def test_current_scope_needs_no_date(self) -> None:
        args = SearchMemoryArgs(query="x", temporal_scope="current")
        assert args.as_of is None

    def test_top_k_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            SearchMemoryArgs(query="x", top_k=1000)


class TestProposeMemoryUpdateArgs:
    def test_relationship_endpoint_must_be_declared(self) -> None:
        """Mirrors CandidateMemory's own rule exactly -- an endpoint absent
        from `entities` must be rejected, never silently create one.
        """
        with pytest.raises(ValidationError):
            ProposeMemoryUpdateArgs(
                statement="X uses Y",
                memory_type="semantic",
                persistence="persistent",
                confidence=0.9,
                entities=[CandidateEntity(name="X", entity_type="project")],
                relationships=[CandidateRelationship(source="X", predicate="uses", target="Y")],
                reasoning="test",
            )

    def test_valid_proposal_round_trips(self) -> None:
        args = ProposeMemoryUpdateArgs(
            statement="X uses Y",
            memory_type="semantic",
            persistence="persistent",
            confidence=0.9,
            entities=[
                CandidateEntity(name="X", entity_type="project"),
                CandidateEntity(name="Y", entity_type="technology"),
            ],
            relationships=[CandidateRelationship(source="X", predicate="uses", target="Y")],
            reasoning="the user just said this",
        )
        assert args.statement == "X uses Y"


class TestAgentStepDiscriminatedUnion:
    def test_tool_call_step_round_trips(self) -> None:
        step = AgentStep(
            step=ToolCallStep(tool_name="search_memory", tool_args_json='{"query": "hi"}')
        )
        parsed = AgentStep.model_validate_json(step.model_dump_json())
        assert isinstance(parsed.step, ToolCallStep)
        assert parsed.step.tool_name == "search_memory"
        assert parsed.step.tool_args_json == '{"query": "hi"}'

    def test_final_answer_step_round_trips(self) -> None:
        step = AgentStep(
            step=FinalAnswerStep(answer="hello", cited_memory_ids=["m1"], confidence="high")
        )
        parsed = AgentStep.model_validate_json(step.model_dump_json())
        assert isinstance(parsed.step, FinalAnswerStep)
        assert parsed.step.cited_memory_ids == ["m1"]

    def test_unknown_tool_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentStep.model_validate(
                {
                    "step": {
                        "action": "tool_call",
                        "tool_name": "run_arbitrary_cypher",
                        "tool_args_json": "{}",
                    }
                }
            )

    def test_final_answer_confidence_defaults_to_medium(self) -> None:
        step = FinalAnswerStep(answer="hi")
        assert step.confidence == "medium"


class TestGeminiStructuredOutputCompatibility:
    """Two real bugs found running Phase 5's evaluation against the real
    Gemini Developer API -- neither caught by any test that only exercised
    `FakeLLMClient`, since `model_validate_json`/`model_dump_json` don't care
    how a schema is shaped, only real structured-output mode does.

    1. An OPEN `dict`-typed field (`ToolCallStep` used to have
       `tool_args: dict[str, Any]`) emits `additionalProperties` with a
       value (`true` or a nested schema); the `google-genai` SDK itself
       rejects this client-side before any network call
       ("additionalProperties is only supported in Gemini Enterprise Agent
       Platform mode, not in Gemini Developer API mode"). Fixed by
       `tool_args_json: str` -- a JSON-encoded string has no
       `additionalProperties` in its own schema at all.

    2. Separately, the real Gemini API rejects `additionalProperties`
       appearing ANYWHERE in the schema tree with ANY value, including
       `false` -- which Pydantic emits for every `extra="forbid"` model in
       this codebase (untrusted-LLM-output validation strictness, not
       something to relax). This is NOT fixable at the model-definition
       level without weakening that strictness, so it is fixed one layer
       down, in the Gemini adapter itself (`app.llm.providers.gemini
       ._to_gemini_schema`, tested in `tests/unit/test_llm_gemini.py`) --
       this file only asserts what the model layer's OWN schema still
       legitimately contains (`additionalProperties: false` is correct and
       expected here; it is the adapter's job to strip it before sending).

    A third, related bug: `AgentStep.step`'s original
    `Field(discriminator="action")` rendered as JSON Schema's
    `discriminator` + `oneOf` keywords, which Gemini's `Schema` type (an
    OpenAPI SUBSET) rejects outright with a client-side
    `pydantic.ValidationError: Extra inputs are not permitted`. Fixed by
    dropping the discriminator -- a plain union renders as `anyOf`, which
    Gemini supports, and Pydantic's "smart mode" union resolution still
    picks the right member unambiguously.
    """

    def _assert_no_open_additional_properties(self, node: object) -> None:
        if isinstance(node, dict):
            value = node.get("additionalProperties")
            assert value is None or value is False, f"open additionalProperties found: {node}"
            for v in node.values():
                self._assert_no_open_additional_properties(v)
        elif isinstance(node, list):
            for item in node:
                self._assert_no_open_additional_properties(item)

    def test_agent_step_schema_has_no_open_additional_properties(self) -> None:
        self._assert_no_open_additional_properties(AgentStep.model_json_schema())

    def test_every_tool_args_schema_has_no_open_additional_properties(self) -> None:
        for schema in TOOL_ARGS_SCHEMAS.values():
            self._assert_no_open_additional_properties(schema.model_json_schema())

    def test_agent_step_union_has_no_discriminator_or_oneof(self) -> None:
        step_schema = AgentStep.model_json_schema()["properties"]["step"]
        assert "discriminator" not in step_schema
        assert "oneOf" not in step_schema
        assert "anyOf" in step_schema, "expected a plain (non-discriminated) union"
