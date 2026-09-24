"""Request/response schemas for the Phase 5 agent endpoint (docs/PHASE5_PLAN.md)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import Message


class AgentTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class AgentTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The persisted assistant message (role=ASSISTANT), same shape every
    #: other conversation message endpoint returns.
    message: Message
    cited_memory_ids: list[str]
    tool_call_count: int
    termination_reason: str
    confidence: str
