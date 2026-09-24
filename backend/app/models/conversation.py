"""Conversation and message domain models.

These are PostgreSQL-backed application state (docs/adr/0006), not canonical memory
-- they double as API response models, matching the Phase 1 convention in
`app/models/domain.py`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ExtractionStatus(StrEnum):
    """Per-message extraction bookkeeping (`messages.extraction_status`)."""

    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    conversation_id: str
    user_id: str
    role: MessageRole
    content: str
    sequence: int
    token_count: int | None = None
    created_at: datetime
    extraction_status: ExtractionStatus
    extraction_gate_reason: str | None = None


class ConversationDetail(BaseModel):
    """A conversation with its messages, for the "retrieve conversation" endpoint."""

    model_config = ConfigDict(extra="forbid")

    conversation: Conversation
    messages: list[Message] = Field(default_factory=list)
