"""The LLM extraction contract (docs/adr/0007, docs/PHASE2_PLAN.md section 5).

This is the *only* shape an LLM's output may take. Three properties are
non-negotiable:

* **No identifiers.** Entities and relationship endpoints are given by *name*,
  never by id. Temporal replacement is a natural-language hint
  (`replaces_hint: "EasyOCR"`), never a target memory id. The model cannot address
  an existing record.
* **No imperatives.** There is no field meaning "supersede", "delete" or "merge".
  `is_change` is an *observation*; the deterministic decision engine
  (`app/extraction/decisions.py`) chooses the operation.
* **Mandatory grounding.** Every candidate carries `source_span`, offsets into the
  source message. `app/extraction/grounding.py` verifies the span exists and that
  claimed entity mentions actually occur within it -- a fabricated fact with no
  textual anchor is rejected deterministically, not argued with.

Untrusted input: this is exactly what a provider returns as JSON, so every field is
validated strictly (`extra="forbid"`, bounded lengths, bounded lists) before
anything downstream ever sees it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import EntityType, MemoryType
from app.models.normalization import PREDICATE_PATTERN

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

#: Persistence is a candidate's own self-assessment, checked (not trusted) by the
#: deterministic gate and decision engine downstream.
Persistence = Literal["persistent", "temporary", "uncertain"]


class Span(BaseModel):
    """Character offsets into one message's `content`. `start` inclusive, `end`
    exclusive -- the same half-open convention as the temporal model
    (docs/adr/0002), for the same reason: it composes without off-by-one errors.
    """

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> Span:
        if self.end <= self.start:
            raise ValueError("span end must be strictly after span start")
        return self


class CandidateEntity(BaseModel):
    """An entity the model believes the message mentions. NAME, never an id."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    entity_type: EntityType
    aliases: list[str] = Field(default_factory=list, max_length=10)
    mention_span: Span | None = None


class CandidateRelationship(BaseModel):
    """An asserted edge between two of this candidate's entities, by NAME."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=200)
    #: Same shape Phase 1's RelationshipInput enforces (app.models.normalization),
    #: rejected here rather than discovered later when the runner tries to persist.
    predicate: str = Field(pattern=PREDICATE_PATTERN)
    target: str = Field(min_length=1, max_length=200)
    confidence: Confidence = 1.0


class CandidateTemporal(BaseModel):
    """Temporal signals as observations, never as instructions.

    `replaces_hint` names an entity/technology in free text ("EasyOCR") -- it is
    resolved to an existing edge by the decision engine, not treated as a
    reference to a specific stored memory or relationship id.
    """

    model_config = ConfigDict(extra="forbid")

    valid_from_hint: str | None = Field(default=None, max_length=200)
    valid_until_hint: str | None = Field(default=None, max_length=200)
    is_change: bool = False
    replaces_hint: str | None = Field(default=None, max_length=200)


class CandidateMemory(BaseModel):
    """One candidate fact proposed by the model. Not yet a Memory -- see
    `app/extraction/decisions.py` for what happens between here and Neo4j.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=2000)
    memory_type: MemoryType
    persistence: Persistence
    confidence: Confidence
    entities: list[CandidateEntity] = Field(default_factory=list, max_length=20)
    relationships: list[CandidateRelationship] = Field(default_factory=list, max_length=20)
    temporal: CandidateTemporal = Field(default_factory=CandidateTemporal)
    source_message_id: str = Field(min_length=1, max_length=64)
    source_span: Span
    reasoning: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _relationship_endpoints_are_declared(self) -> CandidateMemory:
        """A relationship must name entities this same candidate declared.

        Mirrors the Phase 1 rule in `MemoryService._build_relationships`: an
        endpoint absent from `entities` cannot silently create a new one.
        """
        known = {e.name.strip().lower() for e in self.entities}
        for rel in self.relationships:
            for endpoint in (rel.source, rel.target):
                if endpoint.strip().lower() not in known:
                    raise ValueError(
                        f"relationship endpoint {endpoint!r} is not listed in "
                        f"this candidate's entities"
                    )
        return self


class ExtractionResult(BaseModel):
    """The complete output of one extraction call."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateMemory] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------------------
# Job and candidate persistence (PostgreSQL, docs/adr/0006). These are NOT the LLM
# contract above -- they are what `app/repositories/extraction_repository.py`
# stores about a job's lifecycle and each candidate's fate, exposed through
# GET /extraction/status.
# ---------------------------------------------------------------------------


class DecisionType(StrEnum):
    """What the deterministic decision engine did with a candidate
    (app/extraction/decisions.py, docs/PHASE2_PLAN.md section 4).
    """

    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    UPDATE = "UPDATE"
    SUPERSEDING = "SUPERSEDING"
    CONFLICTING = "CONFLICTING"
    INVALID = "INVALID"
    IGNORE = "IGNORE"
    #: Phase 3 (ADR 0009, PHASE3_PLAN.md section 5): semantic evidence, never
    #: exact-triple matching, produced these. Both persist the candidate as a
    #: NEW memory in Neo4j -- neither ever merges or overwrites -- and differ
    #: only in how strong the evidence was (app.semantic.similarity).
    LIKELY_DUPLICATE = "LIKELY_DUPLICATE"
    FLAGGED = "FLAGGED"


class ExtractionJobStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ExtractionJob(BaseModel):
    """One unit of extraction work over a window of messages."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    conversation_id: str
    target_message_id: str
    window_start_sequence: int
    window_end_sequence: int
    extractor_version: str
    status: ExtractionJobStatus
    attempts: int
    locked_at: datetime | None = None
    locked_by: str | None = None
    next_attempt_at: datetime | None = None
    error_code: str | None = None
    error_detail: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    candidate_count: int | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ExtractionCandidateRecord(BaseModel):
    """What the model proposed, and what the engine decided -- the audit row.

    **Not canonical memory.** `memory_id` is an opaque pointer into Neo4j, never a
    copy of memory state (docs/adr/0006).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    extraction_job_id: str
    user_id: str
    source_message_id: str
    ordinal: int
    candidate_hash: str
    payload: dict[str, Any]
    decision: DecisionType
    decision_reason: str
    confidence: float | None = None
    memory_id: str | None = None
    applied_at: datetime | None = None
    created_at: datetime
