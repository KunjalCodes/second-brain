"""Project-history domain models (Phase 16): agent events, sessions and Git commits.

PostgreSQL-backed application state (docs/adr/0006), exposed as API models like the
conversation models. Free text in any of these (event payloads, commit messages,
file names) is UNTRUSTED data: it is returned as data, never interpreted.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentEventType(StrEnum):
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    USER_REQUEST = "user_request"
    DECISION = "decision"
    FILE_INSPECTED = "file_inspected"
    FILE_MODIFIED = "file_modified"
    BUG_DISCOVERED = "bug_discovered"
    ERROR = "error"
    FIX = "fix"
    TEST_RUN = "test_run"
    COMMIT = "commit"
    TASK_COMPLETED = "task_completed"
    TASK_BLOCKED = "task_blocked"
    #: A structured memory the agent PROPOSES; it goes through full governance.
    MEMORY_PROPOSAL = "memory_proposal"


ProjectionStatus = Literal["pending", "projected", "skipped", "failed"]


class GitFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    #: Git name-status letter: A(dded), M(odified), D(eleted), R(enamed), C(opied), T(ype).
    status: Literal["A", "M", "D", "R", "C", "T"]
    additions: int | None = Field(default=None, ge=0)
    deletions: int | None = Field(default=None, ge=0)
    old_path: str | None = Field(default=None, max_length=512)


class GitCommitSummary(BaseModel):
    """What a provenance reader needs to see for a `git_commit` source."""

    model_config = ConfigDict(extra="forbid")

    id: str
    sha: str
    #: UNTRUSTED text from the repository.
    subject: str
    author_name: str | None = None
    committed_at: datetime
    files_changed_count: int
    #: For a `file:<path>` location: that file's change in this commit, if recorded.
    file_change: GitFileChange | None = None


class AgentEventSummary(BaseModel):
    """What a provenance reader needs to see for an `agent_event` source."""

    model_config = ConfigDict(extra="forbid")

    id: str
    event_type: AgentEventType
    session_key: str
    agent_name: str | None = None
    occurred_at: datetime
    recorded_at: datetime
    #: UNTRUSTED text the agent supplied (the event's primary text field).
    text: str | None = None


#: The payload key that carries each event type's primary human-readable text.
#: One mapping, used by provenance, history and resume views, so they all agree on
#: what an event "says". The payload schemas (`app/projects/events.py`) define it.
PRIMARY_TEXT_KEY: dict[AgentEventType, str] = {
    AgentEventType.SESSION_STARTED: "goal",
    AgentEventType.SESSION_ENDED: "summary",
    AgentEventType.USER_REQUEST: "text",
    AgentEventType.DECISION: "title",
    AgentEventType.FILE_INSPECTED: "path",
    AgentEventType.FILE_MODIFIED: "path",
    AgentEventType.BUG_DISCOVERED: "title",
    AgentEventType.ERROR: "message",
    AgentEventType.FIX: "title",
    AgentEventType.TEST_RUN: "command",
    AgentEventType.COMMIT: "message",
    AgentEventType.TASK_COMPLETED: "summary",
    AgentEventType.TASK_BLOCKED: "reason",
    AgentEventType.MEMORY_PROPOSAL: "statement",
}


def event_text(event_type: AgentEventType, payload: dict) -> str | None:
    value = payload.get(PRIMARY_TEXT_KEY[event_type])
    return value if isinstance(value, str) else None
