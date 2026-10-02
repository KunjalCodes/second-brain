"""Shaping existing graph/retrieval results for the project-memory API (Phase 16)."""

from __future__ import annotations

from app.core.errors import ValidationError
from app.models.domain import Source
from app.models.enums import SourceType
from app.projects.naming import (
    branch_name,
    clean_text,
    commit_name,
    directory_name,
    file_name,
    session_name,
    symbol_name,
    titled_name,
)
from app.schemas.project_query import Evidence, SourceRef

#: What a caller may name. `ref` is the path / hash / key / title, never a prefixed
#: entity name: the server applies the prefix, so a caller cannot reach an entity of
#: another kind by spelling its name.
ENTITY_KINDS = (
    "file", "directory", "commit", "session", "branch", "symbol", "decision", "feature",
    "bug", "error", "fix", "dependency", "name",
)  # fmt: skip


def entity_name_for(kind: str, ref: str) -> str:
    if kind == "file":
        return file_name(ref)
    if kind == "directory":
        return directory_name(ref)
    if kind == "commit":
        return commit_name(ref) if len(ref) >= 40 else _short_commit(ref)
    if kind == "session":
        return session_name(ref)
    if kind == "branch":
        return branch_name(ref)
    if kind == "symbol":
        path, sep, qualified = ref.partition("::")
        if not sep:
            raise ValidationError("a symbol ref is '<path>::<qualified name>'")
        return symbol_name(path, qualified)
    if kind in ("decision", "feature", "bug", "error", "fix"):
        return titled_name(kind, ref)
    if kind == "dependency":
        return f"dependency:{clean_text(ref, max_length=100, field='dependency')}"
    if kind == "name":
        return clean_text(ref, max_length=200, field="name")
    raise ValidationError(f"kind must be one of: {', '.join(ENTITY_KINDS)}")


def _short_commit(ref: str) -> str:
    value = ref.strip().lower()
    if not value.isalnum() or len(value) < 7:
        raise ValidationError("commit must be at least 7 hex characters")
    return f"commit:{value[:12]}"


def evidence_labels(sources: list[Source]) -> list[Evidence]:
    labels: list[Evidence] = []
    for s in sources:
        label: Evidence
        if s.source_type is SourceType.GIT_COMMIT:
            label = "parser" if (s.location or "").startswith("file:") else "git"
        elif s.source_type is SourceType.AGENT_EVENT:
            label = "agent_stated"
        else:
            label = "derived"
        if label not in labels:
            labels.append(label)
    return labels


def source_refs(sources: list[Source]) -> list[SourceRef]:
    out = []
    for s in sources:
        if s.reference and s.source_type in (SourceType.GIT_COMMIT, SourceType.AGENT_EVENT):
            out.append(SourceRef(type=s.source_type.value, id=s.reference))  # type: ignore[arg-type]
    return out
