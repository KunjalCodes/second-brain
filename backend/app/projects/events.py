"""Agent event schemas (Phase 16, M7): the ingestion boundary for an external coding
agent (docs/PHASE16_ARCHITECTURE.md section 7).

Properties that make this safe to expose:

* **No authority fields.** Every model is `extra="forbid"`: `user_id`, `project_id`,
  `space_id`, ownership or any database id in an event is a 422. The project comes
  from the credential; the session is looked up inside that project.
* **Typed payloads.** Each event type has its own payload model; unknown fields are
  rejected, text is cleaned (control and bidirectional-override characters removed)
  and bounded, paths are normalised to safe relative paths, lists are capped.
* **Bounded.** The whole payload is limited in size before it is even parsed.
* **Untrusted by construction.** Nothing here interprets a string as an instruction.

Validation is atomic per request: one malformed event rejects the request before any
event is written.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

from app.core.errors import ValidationError as AppValidationError
from app.models.enums import EntityType
from app.models.normalization import PREDICATE_PATTERN
from app.models.project_history import AgentEventType
from app.projects.naming import (
    clean_text,
    normalize_branch,
    normalize_path,
    normalize_session_key,
    normalize_sha,
)

MAX_PAYLOAD_BYTES = 16_000
MAX_EVENTS_PER_REQUEST = 50
EVENT_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


def _wrap(fn):  # noqa: ANN001, ANN202
    """Pydantic only turns ValueError into a 422; our domain error must not escape."""

    def check(value):  # noqa: ANN001, ANN202
        try:
            return fn(value)
        except AppValidationError as exc:
            raise ValueError(exc.message) from None

    return check


def _text(max_length: int):  # noqa: ANN202
    return Annotated[
        str,
        Field(max_length=max_length),
        AfterValidator(_wrap(lambda v: clean_text(v, max_length=max_length))),
    ]


Path = Annotated[str, AfterValidator(_wrap(normalize_path))]
SessionKey = Annotated[str, AfterValidator(_wrap(normalize_session_key))]
Branch = Annotated[str, AfterValidator(_wrap(normalize_branch))]
Sha = Annotated[str, AfterValidator(_wrap(normalize_sha))]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionStartedPayload(_Payload):
    goal: _text(500) | None = None
    #: An EARLIER session of this project that this one continues.
    continues_session: SessionKey | None = None
    branch: Branch | None = None


class SessionEndedPayload(_Payload):
    outcome: Literal["completed", "blocked", "abandoned"]
    summary: _text(2000) | None = None
    #: Explicit unfinished work. The memory engine never infers this.
    unfinished: list[_text(300)] = Field(default_factory=list, max_length=10)


class UserRequestPayload(_Payload):
    text: _text(2000)
    feature: _text(160) | None = None


class DecisionPayload(_Payload):
    title: _text(160)
    #: Stated by the agent. If absent, no rationale is ever fabricated.
    rationale: _text(1000) | None = None
    alternatives: list[_text(200)] = Field(default_factory=list, max_length=5)
    files: list[Path] = Field(default_factory=list, max_length=10)
    feature: _text(160) | None = None
    #: Title of an EARLIER decision this one replaces (explicit).
    supersedes_decision: _text(160) | None = None


class FileInspectedPayload(_Payload):
    path: Path
    note: _text(300) | None = None


class FileModifiedPayload(_Payload):
    path: Path
    change: Literal["created", "modified", "deleted", "renamed"] = "modified"
    old_path: Path | None = None
    summary: _text(300) | None = None

    @model_validator(mode="after")
    def _renamed_needs_old_path(self) -> FileModifiedPayload:
        if self.change == "renamed" and not self.old_path:
            raise ValueError("old_path is required when change is 'renamed'")
        if self.change != "renamed" and self.old_path:
            raise ValueError("old_path is only allowed when change is 'renamed'")
        return self


class BugDiscoveredPayload(_Payload):
    title: _text(160)
    description: _text(1000) | None = None
    files: list[Path] = Field(default_factory=list, max_length=8)
    feature: _text(160) | None = None


class ErrorPayload(_Payload):
    message: _text(1000)
    #: Short label (defaults to the first line of `message`). Equal titles are one error.
    title: _text(160) | None = None
    command: _text(300) | None = None
    files: list[Path] = Field(default_factory=list, max_length=8)
    #: Title of a bug this error is a symptom of (explicit).
    bug: _text(160) | None = None
    #: Label of the approach being tried when this happened (explicit).
    approach: _text(200) | None = None


class FixPayload(_Payload):
    title: _text(160)
    description: _text(1000) | None = None
    #: REQUIRED: whether the fix worked is stated, never inferred.
    outcome: Literal["succeeded", "partial", "failed"]
    fixes_error: _text(160) | None = None
    fixes_bug: _text(160) | None = None
    files: list[Path] = Field(default_factory=list, max_length=8)
    approach: _text(200) | None = None


class TestRunPayload(_Payload):
    __test__ = False  # not a pytest class

    command: _text(300)
    result: Literal["passed", "failed", "error"]
    summary: _text(500) | None = None
    passed: int | None = Field(default=None, ge=0, le=1_000_000)
    failed: int | None = Field(default=None, ge=0, le=1_000_000)
    #: Titles of fixes this run validates (explicit).
    validates: list[_text(160)] = Field(default_factory=list, max_length=5)
    files: list[Path] = Field(default_factory=list, max_length=8)
    approach: _text(200) | None = None


class CommitPayload(_Payload):
    sha: Sha
    message: _text(500)
    files: list[Path] = Field(default_factory=list, max_length=15)


class TaskCompletedPayload(_Payload):
    summary: _text(1000)
    feature: _text(160) | None = None


class TaskBlockedPayload(_Payload):
    reason: _text(1000)
    feature: _text(160) | None = None
    needs: _text(300) | None = None
    approach: _text(200) | None = None


#: Entity types an agent may PROPOSE. Structural types (files, commits, sessions, ...)
#: have exact, deterministic names and come only from Git ingestion and typed events.
PROPOSABLE_PLAIN = frozenset(
    {
        EntityType.PERSON, EntityType.COMPANY, EntityType.TECHNOLOGY, EntityType.CONCEPT,
        EntityType.DOCUMENT, EntityType.EVENT, EntityType.TASK, EntityType.OTHER,
    }
)  # fmt: skip
#: Proposable types whose entity names carry a `<kind>:` prefix, so a proposal about a
#: bug reaches the same entity a BUG_DISCOVERED event created.
PROPOSABLE_TITLED = frozenset(
    {EntityType.DECISION, EntityType.FEATURE, EntityType.BUG, EntityType.ERROR, EntityType.FIX}
)
MAX_PROPOSAL_CONFIDENCE = 0.8


class ProposalEntity(_Payload):
    name: _text(160)
    entity_type: EntityType

    @model_validator(mode="after")
    def _proposable(self) -> ProposalEntity:
        if self.entity_type not in PROPOSABLE_PLAIN | PROPOSABLE_TITLED:
            raise ValueError(
                f"entity_type {self.entity_type.value!r} cannot be proposed: structural entities "
                "come only from Git ingestion and typed events"
            )
        return self


class ProposalRelationship(_Payload):
    source: _text(160)
    predicate: Annotated[str, Field(pattern=PREDICATE_PATTERN, max_length=60)]
    target: _text(160)


class MemoryProposalPayload(_Payload):
    statement: _text(1000)
    memory_type: Literal["episodic", "semantic", "procedural"] = "semantic"
    entities: list[ProposalEntity] = Field(default_factory=list, max_length=10)
    relationships: list[ProposalRelationship] = Field(default_factory=list, max_length=10)
    #: The agent's own confidence; capped (it is metadata, never authority).
    confidence: float = Field(default=0.7, ge=0.0, le=MAX_PROPOSAL_CONFIDENCE)
    reasoning: _text(500) | None = None

    @model_validator(mode="after")
    def _endpoints_declared(self) -> MemoryProposalPayload:
        known = {e.name.lower() for e in self.entities}
        if len(known) != len(self.entities):
            raise ValueError("an entity is listed twice")
        for rel in self.relationships:
            for end in (rel.source, rel.target):
                if end.lower() not in known:
                    raise ValueError(f"relationship endpoint {end!r} is not a listed entity")
        return self


PAYLOAD_MODELS: dict[AgentEventType, type[_Payload]] = {
    AgentEventType.SESSION_STARTED: SessionStartedPayload,
    AgentEventType.SESSION_ENDED: SessionEndedPayload,
    AgentEventType.USER_REQUEST: UserRequestPayload,
    AgentEventType.DECISION: DecisionPayload,
    AgentEventType.FILE_INSPECTED: FileInspectedPayload,
    AgentEventType.FILE_MODIFIED: FileModifiedPayload,
    AgentEventType.BUG_DISCOVERED: BugDiscoveredPayload,
    AgentEventType.ERROR: ErrorPayload,
    AgentEventType.FIX: FixPayload,
    AgentEventType.TEST_RUN: TestRunPayload,
    AgentEventType.COMMIT: CommitPayload,
    AgentEventType.TASK_COMPLETED: TaskCompletedPayload,
    AgentEventType.TASK_BLOCKED: TaskBlockedPayload,
    AgentEventType.MEMORY_PROPOSAL: MemoryProposalPayload,
}


class EventIn(BaseModel):
    """One event as an external agent sends it."""

    model_config = ConfigDict(extra="forbid")

    #: The agent's own session id (its conversation/run id). Scoped to the project.
    session_id: SessionKey
    event_type: AgentEventType
    #: When it happened (valid time). Must carry a timezone.
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Which integration sent it (informational, e.g. "claude-code"). Never an authority.
    source: _text(80) | None = None
    #: Optional client idempotency key. Without it one is derived from the content.
    event_id: Annotated[str, Field(pattern=EVENT_KEY_PATTERN)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _case_insensitive_type(cls, data: Any, info: ValidationInfo) -> Any:  # noqa: ANN401
        if isinstance(data, dict) and isinstance(data.get("event_type"), str):
            data = {**data, "event_type": data["event_type"].strip().lower()}
        return data

    @model_validator(mode="after")
    def _validate_payload(self) -> EventIn:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a timezone (e.g. ...Z)")
        self.timestamp = self.timestamp.astimezone(UTC)
        encoded = json.dumps(self.payload, default=str)
        if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes")
        model = PAYLOAD_MODELS[self.event_type]
        # `exclude_none` keeps the stored payload canonical: absent == omitted.
        self.payload = model.model_validate(self.payload).model_dump(mode="json", exclude_none=True)
        return self


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(min_length=1, max_length=MAX_EVENTS_PER_REQUEST)


def canonical_json(value: Any) -> str:  # noqa: ANN401
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_fingerprint(event: EventIn) -> str:
    """Hash of everything that gives an event its meaning (type, valid time,
    payload). The idempotency key when the client supplies none, and the check that
    a reused client `event_id` really is the same event.

    A MEMORY_PROPOSAL carries a server-assigned time, so its identity is its payload
    alone: a retried proposal (new timestamp, same content) is the same proposal."""
    parts: dict[str, Any] = {"type": event.event_type.value, "payload": event.payload}
    if event.event_type is not AgentEventType.MEMORY_PROPOSAL:
        parts["at"] = event.timestamp.astimezone(UTC).isoformat()
    return hashlib.sha256(canonical_json(parts).encode()).hexdigest()
