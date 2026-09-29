"""`GET /memories/{id}/explanation` -- "why does the system remember this?"
(Phase 14, M17).

Assembled only from stored records: the Neo4j memory and its links, the
`extraction_candidates` audit rows that created or corroborated it, the review
queue, and provenance. Nothing here is generated. `model_reasoning` is the
extraction model's own free text, stored with the candidate -- it is labelled
as model-written and must be rendered as inert text (ADR 0029).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.domain import Memory, Relationship
from app.schemas.provenance import SourceProvenance

_WORTHINESS = re.compile(r"^\[worthiness:([a-z_]+)\]")
_TEMPORAL = re.compile(r"\[temporal: (.+)\]\s*$")

CONFIDENCE_NOTE = (
    "Confidence is the extraction model's own estimate for the statement it "
    "proposed, stored as metadata. It is not a probability that the fact is "
    "true, and it never overrides the sources or the validity window."
)


class ExtractionDecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    #: The deterministic engine's full recorded reason.
    reason: str
    #: Parsed from `reason`: the memory-worthiness rule that let the candidate
    #: through (None for decisions recorded before Phase 14).
    worthiness_rule: str | None
    #: Parsed from `reason`: how validity was interpreted from temporal hints.
    temporal_interpretation: str | None
    #: `creation` for the decision that made the memory, `corroboration` for a
    #: later DUPLICATE/UPDATE that pointed at it.
    role: Literal["creation", "corroboration"]
    persistence: str | None
    model_confidence: float | None
    model_reasoning: str | None
    temporal_hints: dict[str, object]
    source_kind: Literal["message", "document_chunk"]
    source_id: str
    recorded_at: datetime


class ReviewItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    status: str
    other_memory_id: str
    score: float | None
    created_at: datetime


class MemoryExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: Memory
    #: `extraction` when at least one extraction decision is on record; `direct`
    #: for a memory created through `POST /memories` (no extraction trail).
    origin: Literal["extraction", "direct"]
    decisions: list[ExtractionDecisionView]
    sources: list[SourceProvenance]
    relationships: list[Relationship]
    supersedes: list[str]
    superseded_by: list[str]
    conflicts_with: list[str]
    duplicate_of: list[str]
    review: list[ReviewItemView]
    confidence_note: str = CONFIDENCE_NOTE


def parse_reason(reason: str) -> tuple[str | None, str | None]:
    worth = _WORTHINESS.match(reason)
    temporal = _TEMPORAL.search(reason)
    return (worth.group(1) if worth else None, temporal.group(1) if temporal else None)
