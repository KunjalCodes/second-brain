"""Git ingestion schemas (Phase 16, M4/M5; docs/PHASE16_ARCHITECTURE.md section 7).

The server NEVER reads the filesystem or runs `git`. A local read-only collector
(`scripts/git_collect.py`) sends bounded, structured batches; these models are the
boundary. Everything is `extra="forbid"`, typed, size-capped and path-normalised, and
a malformed batch is rejected whole before anything is written.

What is deliberately NOT accepted: diffs, file contents, raw author emails (only a
SHA-256 of the lower-cased address), absolute paths, or any identifier a client could
use as authority.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.projects.events import Branch, Path, Sha, _text
from app.projects.identity import MAX_ROOT_COMMITS

MAX_COMMITS_PER_REQUEST = 50
MAX_FILES_PER_COMMIT = 200
MAX_STRUCTURE_FILES_PER_REQUEST = 100
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


Aware = Annotated[datetime, AfterValidator(_aware)]


def _email_hash(value: str) -> str:
    if not _HEX64.match(value):
        raise ValueError("author_email_sha256 must be a lower-case SHA-256 hex digest")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryIdentityIn(_Strict):
    """Echoed from the local repository so the server can refuse a batch that belongs
    to a different repository than the project (a safety check, not authentication)."""

    repository_url: str | None = Field(default=None, max_length=300)
    root_commits: list[str] | None = Field(default=None, max_length=MAX_ROOT_COMMITS)


class BranchIn(_Strict):
    name: Branch
    tip: Sha


class CommitFileIn(_Strict):
    path: Path
    status: Literal["A", "M", "D", "R", "C", "T"]
    additions: int | None = Field(default=None, ge=0, le=10_000_000)
    deletions: int | None = Field(default=None, ge=0, le=10_000_000)
    old_path: Path | None = None

    @model_validator(mode="after")
    def _rename_shape(self) -> CommitFileIn:
        if self.status in ("R", "C") and not self.old_path:
            raise ValueError("old_path is required for renames and copies")
        if self.status not in ("R", "C") and self.old_path:
            raise ValueError("old_path is only allowed for renames and copies")
        return self


class CommitIn(_Strict):
    sha: Sha
    parents: list[Sha] = Field(default_factory=list, max_length=8)
    author_name: _text(200) | None = None
    author_email_sha256: Annotated[str, AfterValidator(_email_hash)] | None = None
    authored_at: Aware | None = None
    committed_at: Aware
    subject: _text(500)
    body: _text(4000) | None = None
    body_truncated: bool = False
    #: Branches (of at most 20) that contain this commit, as the collector saw them.
    branches: list[Branch] = Field(default_factory=list, max_length=20)
    files: list[CommitFileIn] = Field(default_factory=list, max_length=MAX_FILES_PER_COMMIT)
    #: The TRUE number of changed files; `files` may be shorter (truncated / unrepresentable).
    files_changed_count: int = Field(ge=0, le=10_000_000)
    files_truncated: bool = False
    additions: int | None = Field(default=None, ge=0, le=100_000_000)
    deletions: int | None = Field(default=None, ge=0, le=100_000_000)

    @model_validator(mode="after")
    def _counts(self) -> CommitIn:
        if self.files_changed_count < len(self.files):
            raise ValueError("files_changed_count cannot be less than the files listed")
        if len(self.files) < self.files_changed_count and not self.files_truncated:
            raise ValueError("files_truncated must be true when files is incomplete")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("a path appears twice in files")
        return self


class GitBatch(_Strict):
    identity: RepositoryIdentityIn
    branches: list[BranchIn] = Field(default_factory=list, max_length=20)
    commits: list[CommitIn] = Field(min_length=1, max_length=MAX_COMMITS_PER_REQUEST)


class SymbolIn(_Strict):
    name: _text(100)
    kind: Literal["class", "function", "method"]
    line: int = Field(ge=1, le=10_000_000)


_DEP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,99}$")


def _dep(value: str) -> str:
    if not _DEP.match(value):
        raise ValueError("dependency name is not a plain package identifier")
    return value


class StructureFileIn(_Strict):
    path: Path
    language: _text(40)
    size_bytes: int | None = Field(default=None, ge=0, le=10**10)
    symbols: list[SymbolIn] = Field(default_factory=list, max_length=100)
    #: Repository-internal files this file imports (resolved by the collector).
    imports: list[Path] = Field(default_factory=list, max_length=100)
    #: External packages this file imports (top-level names).
    dependencies: list[Annotated[str, AfterValidator(_dep)]] = Field(
        default_factory=list, max_length=50
    )
    symbols_truncated: bool = False


class StructureBatch(_Strict):
    identity: RepositoryIdentityIn
    #: The commit this snapshot was read from. Must already be ingested: every
    #: structural fact cites it as its Git evidence.
    commit_sha: Sha
    files: list[StructureFileIn] = Field(
        min_length=1, max_length=MAX_STRUCTURE_FILES_PER_REQUEST
    )
