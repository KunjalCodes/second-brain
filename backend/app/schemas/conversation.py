"""Request and response schemas for the conversation API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import Conversation, ConversationDetail, Message, MessageRole


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole = MessageRole.USER
    content: str = Field(min_length=1, max_length=20_000)


class ConversationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Conversation]
    total: int
    limit: int
    offset: int


class MessageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Message]
    limit: int
    offset: int


__all__ = [
    "Conversation",
    "ConversationCreate",
    "ConversationDetail",
    "ConversationListResponse",
    "Message",
    "MessageCreate",
    "MessageListResponse",
]
