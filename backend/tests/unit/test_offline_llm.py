"""Phase 11: `LLM_PROVIDER=fake` in a running app answers honestly instead of
raising (M0 measured every extraction job failing and every agent turn 500)."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import LLMUnavailableError
from app.llm.factory import build_llm_client
from app.llm.fake import OFFLINE_AGENT_ANSWER, OfflineLLMClient
from app.models.agent import AgentStep, FinalAnswerStep
from app.models.extraction import ExtractionResult
from app.schemas.memory import MemoryCreate


async def test_factory_uses_the_offline_client_for_fake() -> None:
    assert isinstance(build_llm_client(Settings(llm_provider="fake")), OfflineLLMClient)


async def test_extraction_yields_no_candidates() -> None:
    result = await OfflineLLMClient().complete_structured(prompt="x", schema=ExtractionResult)
    assert result.value.candidates == []
    assert result.provider == "offline"


async def test_agent_step_is_an_honest_final_answer_citing_nothing() -> None:
    result = await OfflineLLMClient().complete_structured(prompt="x", schema=AgentStep)
    step = result.value.step
    assert isinstance(step, FinalAnswerStep)
    assert step.answer == OFFLINE_AGENT_ANSWER
    assert step.cited_memory_ids == []


async def test_unknown_schema_is_unavailable_not_invented() -> None:
    with pytest.raises(LLMUnavailableError):
        await OfflineLLMClient().complete_structured(prompt="x", schema=MemoryCreate)
