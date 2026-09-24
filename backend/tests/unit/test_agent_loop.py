"""The Phase 5 agent loop (docs/PHASE5_PLAN.md sections 5-9) -- pure control
flow, no database. `dispatch_tool` is monkeypatched to a scripted stub so
these tests exercise budget/timeout/loop-detection/repair/citation logic in
complete isolation from the real tools (which are integration-tested
separately against real Neo4j/PostgreSQL in test_agent_tools.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.tools import ToolResult, ToolStatus
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.llm.fake import FakeLLMClient
from app.models.agent import AgentStep, FinalAnswerStep, ToolCallStep
from app.services import agent_service as agent_service_module
from app.services.agent_service import AgentService

USER_ID = "user-a"


def _step(inner: ToolCallStep | FinalAnswerStep) -> AgentStep:
    return AgentStep(step=inner)


def _tool_call(name: str = "search_memory", args: dict | None = None) -> AgentStep:
    return _step(ToolCallStep(tool_name=name, tool_args_json=json.dumps(args or {"query": "x"})))


def _final(answer: str = "the answer", cited: list[str] | None = None) -> AgentStep:
    return _step(FinalAnswerStep(answer=answer, cited_memory_ids=cited or []))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        agent_max_tool_calls=3, agent_tool_timeout_seconds=5, agent_turn_timeout_seconds=5
    )


def _patch_dispatch(monkeypatch, result_or_results):  # noqa: ANN001
    """Replaces `dispatch_tool` with a stub returning scripted `ToolResult`s
    in order (or the same one every time), recording every call it received.
    """
    calls: list[tuple[str, dict]] = []
    results = list(result_or_results) if isinstance(result_or_results, list) else None

    async def fake_dispatch(tool_name, raw_args, *, user_id, deps, timeout):  # noqa: ANN001
        calls.append((tool_name, raw_args))
        if results is not None:
            return results.pop(0)
        return result_or_results

    monkeypatch.setattr(agent_service_module, "dispatch_tool", fake_dispatch)
    return calls


class TestNoUnnecessaryToolCalls:
    async def test_answers_directly_when_no_tool_needed(self, settings, monkeypatch) -> None:  # noqa: ANN001
        calls = _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.OK))
        llm = FakeLLMClient(response=_final("HNSW is an approximate nearest-neighbor index."))
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="What is HNSW?", deps=None
        )
        assert result.tool_call_count == 0
        assert calls == []
        assert result.termination_reason == "final_answer"
        assert result.answer == "HNSW is an approximate nearest-neighbor index."


class TestToolCallThenAnswer:
    async def test_calls_a_tool_then_answers_citing_its_result(self, settings, monkeypatch) -> None:  # noqa: ANN001
        _patch_dispatch(
            monkeypatch,
            ToolResult(
                status=ToolStatus.OK, data={"memories": [{"memory_id": "m1"}]}, memory_ids=["m1"]
            ),
        )
        llm = FakeLLMClient(responses=[_tool_call(), _final("PaddleOCR", cited=["m1"])])
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID,
            conversation_window_text="",
            user_message="what OCR do I use?",
            deps=None,
        )
        assert result.tool_call_count == 1
        assert result.termination_reason == "final_answer"
        assert result.cited_memory_ids == ["m1"]


class TestToolBudget:
    async def test_stops_at_max_tool_calls_with_a_forced_answer(
        self, settings, monkeypatch
    ) -> None:  # noqa: ANN001
        _patch_dispatch(
            monkeypatch,
            ToolResult(status=ToolStatus.OK, memory_ids=["m1"]),
        )
        # Always proposes a DIFFERENT tool call (different query each time) so
        # loop detection never triggers -- this test is specifically about
        # the max-tool-calls budget, not loop detection.
        responses = [_tool_call(args={"query": f"q{i}"}) for i in range(10)]
        llm = FakeLLMClient(responses=responses)
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.tool_call_count == settings.agent_max_tool_calls
        assert result.termination_reason == "budget_exhausted"
        assert result.answer  # a controlled message, never empty


class TestLoopDetection:
    async def test_identical_repeated_tool_call_stops_the_loop(self, settings, monkeypatch) -> None:  # noqa: ANN001
        calls = _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.NOT_FOUND))
        llm = FakeLLMClient(response=_tool_call(args={"query": "same"}))
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "loop_detected"
        assert len(calls) == 1, "the second identical call must never actually dispatch"


class TestMalformedOutputRepair:
    async def test_repair_succeeds_after_one_malformed_response(
        self, settings, monkeypatch
    ) -> None:  # noqa: ANN001
        _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.OK))
        # FakeLLMClient's own construction only supports one scripted style at
        # a time; script the malformed-then-valid sequence via respond_fn.
        attempts = {"n": 0}

        def respond(prompt: str):  # noqa: ANN001
            attempts["n"] += 1
            if attempts["n"] == 1:
                return '{"step": {"action": "not_a_real_action"}}'
            return _final("recovered answer")

        llm = FakeLLMClient(respond_fn=respond)
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "final_answer"
        assert result.answer == "recovered answer"
        assert attempts["n"] == 2, "exactly one repair attempt, not more"

    async def test_gives_up_after_repair_also_fails(self, settings, monkeypatch) -> None:  # noqa: ANN001
        _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.OK))
        llm = FakeLLMClient(raw_override='{"step": {"action": "nonsense"}}')
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "malformed_output"
        assert result.answer  # a controlled message, never a crash


class TestCitationValidation:
    async def test_valid_citation_of_a_retrieved_memory_passes_through(
        self,
        settings,
        monkeypatch,  # noqa: ANN001
    ) -> None:
        _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.OK, memory_ids=["m1", "m2"]))
        llm = FakeLLMClient(responses=[_tool_call(), _final("answer", cited=["m1"])])
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "final_answer"
        assert result.cited_memory_ids == ["m1"]

    async def test_no_citation_when_no_memory_was_retrieved(self, settings, monkeypatch) -> None:  # noqa: ANN001
        llm = FakeLLMClient(response=_final("I don't have that information.", cited=[]))
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "final_answer"
        assert result.cited_memory_ids == []

    async def test_citation_to_an_unretrieved_memory_triggers_repair_and_succeeds(
        self,
        settings,
        monkeypatch,  # noqa: ANN001
    ) -> None:
        _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.OK, memory_ids=["m1"]))
        attempts = {"n": 0}

        def respond(prompt: str):  # noqa: ANN001
            attempts["n"] += 1
            if attempts["n"] == 1:
                return _tool_call()
            if attempts["n"] == 2:
                return _final("bad citation", cited=["m1", "m999-never-retrieved"])
            return _final("corrected", cited=["m1"])

        llm = FakeLLMClient(respond_fn=respond)
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "final_answer"
        assert result.cited_memory_ids == ["m1"]
        assert result.answer == "corrected"

    async def test_citation_still_invalid_after_repair_is_stripped_not_accepted(
        self,
        settings,
        monkeypatch,  # noqa: ANN001
    ) -> None:
        _patch_dispatch(monkeypatch, ToolResult(status=ToolStatus.OK, memory_ids=["m1"]))
        attempts = {"n": 0}

        def respond(prompt: str):  # noqa: ANN001
            attempts["n"] += 1
            if attempts["n"] == 1:
                return _tool_call()
            # every subsequent answer still cites a hallucinated id
            return _final("still bad", cited=["m1", "m999-never-retrieved"])

        llm = FakeLLMClient(respond_fn=respond)
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "citation_stripped"
        assert result.cited_memory_ids == ["m1"], "the hallucinated id must never survive"

    async def test_multiple_retrieved_memories_can_all_be_cited(
        self,
        settings,
        monkeypatch,  # noqa: ANN001
    ) -> None:
        _patch_dispatch(
            monkeypatch, ToolResult(status=ToolStatus.OK, memory_ids=["m1", "m2", "m3"])
        )
        llm = FakeLLMClient(responses=[_tool_call(), _final("answer", cited=["m1", "m2", "m3"])])
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "final_answer"
        assert set(result.cited_memory_ids) == {"m1", "m2", "m3"}


class TestTurnTimeout:
    async def test_turn_timeout_returns_a_controlled_response(self, monkeypatch) -> None:  # noqa: ANN001
        settings = Settings(
            agent_max_tool_calls=6, agent_tool_timeout_seconds=5, agent_turn_timeout_seconds=0.05
        )
        # FakeLLMClient's respond_fn is synchronous; simulate slowness by
        # monkeypatching complete_structured directly instead.
        llm = FakeLLMClient(response=_final("irrelevant"))

        async def slow_complete_structured(
            *, prompt, schema, temperature=0.0, max_tokens=None, timeout=None
        ):
            await asyncio.sleep(1)
            raise AssertionError("should have been cancelled by the turn timeout")

        monkeypatch.setattr(llm, "complete_structured", slow_complete_structured)
        service = AgentService(llm_client=llm, settings=settings)

        result = await service.run_turn(
            user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
        )
        assert result.termination_reason == "turn_timeout"


class TestAgentDisabled:
    async def test_disabled_agent_raises_configuration_error(self) -> None:
        settings = Settings(agent_enabled=False)
        llm = FakeLLMClient(response=_final("x"))
        service = AgentService(llm_client=llm, settings=settings)

        with pytest.raises(ConfigurationError):
            await service.run_turn(
                user_id=USER_ID, conversation_window_text="", user_message="?", deps=None
            )
