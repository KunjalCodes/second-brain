"""Request/response schemas for `/projects/*` (Phase 16).

Every request model is `extra="forbid"`: a client-supplied `user_id`, `space_id`,
`owner_user_id` or `project_id` in a body is a 422, never an authority. The
`space_id` is deliberately absent from every response: it is an internal storage
key, not something a client needs or may use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.project_history import AgentEventType, ProjectionStatus

TokenScope = Literal["read", "ingest", "propose"]


class ProjectRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    #: A REMOTE url (https/ssh/scp-style). Filesystem paths are rejected.
    repository_url: str | None = Field(default=None, max_length=300)
    #: Root commit hash(es), for a repository with no remote.
    root_commits: list[str] | None = Field(default=None, max_length=8)
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)


class ProjectView(BaseModel):
    id: str
    name: str
    identity_kind: Literal["remote_url", "root_commits"]
    #: First 12 hex chars of the identity key: enough to tell projects apart.
    identity_fingerprint: str
    default_branch: str | None
    status: Literal["active", "archived"]
    created_at: datetime


class ProjectRegisterResponse(BaseModel):
    project: ProjectView
    #: False when the same repository identity was already registered by this account.
    created: bool


class ProjectListResponse(BaseModel):
    items: list[ProjectView]
    limit: int
    offset: int


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    scopes: list[TokenScope] = Field(min_length=1, max_length=3)
    #: Lower than the server default is allowed; higher is clamped by validation.
    ttl_days: int | None = Field(default=None, ge=1, le=365)


class TokenView(BaseModel):
    """A token's metadata. The secret itself is never part of this model."""

    id: str
    label: str
    scopes: list[TokenScope]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreated(BaseModel):
    token: TokenView
    #: Shown exactly once. Not stored; not retrievable later.
    secret: str


class TokenListResponse(BaseModel):
    items: list[TokenView]


class EventResult(BaseModel):
    #: The server's id for the recorded event (use it with `get_source`).
    event_id: str
    session_id: str
    event_type: AgentEventType
    #: `recorded` (new) or `duplicate` (this exact event was already recorded).
    status: Literal["recorded", "duplicate"]
    #: Whether the event has become canonical memory yet. `pending` means it is safely
    #: recorded and will be projected (eventual consistency); never lost.
    projection_status: ProjectionStatus
    memory_id: str | None = None
    note: str | None = None


class EventsResponse(BaseModel):
    results: list[EventResult]


class ProjectBacklogResponse(BaseModel):
    attempted: int
    projected: int
    still_pending: int


class CommitResultView(BaseModel):
    sha: str
    status: Literal["recorded", "duplicate"]
    projection_status: ProjectionStatus
    note: str | None = None


class GitCommitsResponse(BaseModel):
    results: list[CommitResultView]


class StructureResponse(BaseModel):
    files_received: int
    memories_created: int
    memories_existing: int
    memories_superseded: int
    #: Files skipped because a NEWER snapshot of them is already recorded.
    files_stale: int
