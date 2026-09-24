"""Response schema for `GET /memories/{id}/provenance`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.conversation import Message
from app.models.domain import Memory, Source


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Source
    #: Resolved from Neo4j's Source.reference into the actual PostgreSQL message,
    #: when the source is a conversation message and that message still exists.
    #: None for non-message sources or if the message could not be resolved.
    message: Message | None = None


class ProvenanceView(BaseModel):
    """Answers "why does the system believe this?" -- Memory -> Source -> Message."""

    model_config = ConfigDict(extra="forbid")

    memory: Memory
    sources: list[SourceProvenance]
