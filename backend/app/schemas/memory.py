"""Request and response schemas for the memory API.

Domain models from `app.models.domain` are reused directly as response bodies; only
request bodies and list envelopes are defined here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.domain import Entity, Memory, MemoryDetail
from app.models.enums import EntityType, MemoryStatus, MemoryType, SourceType
from app.models.normalization import PREDICATE_PATTERN

#: Predicates are stored verbatim as a relationship property. Constrained to a
#: conservative identifier shape so that they stay readable, comparable and safe to
#: display, without restricting them to a fixed vocabulary (extraction in later
#: phases must be free to produce specific predicates -- CLAUDE.md section 8).
#: Re-exported here for anything importing PREDICATE_PATTERN from this module's
#: previous location; the definition itself lives in app.models.normalization so
#: app.models.extraction can share it without app/models importing app/schemas.

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

#: Statuses a caller may assign when *creating* a memory. `superseded` and `invalid`
#: are reachable only through the operations that also close the affected edges, and
#: `archived` only through an update, so that the graph cannot be put into a state
#: where a memory claims to be superseded with nothing superseding it.
CREATABLE_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.ACTIVE, MemoryStatus.HISTORICAL, MemoryStatus.CONFLICTING}
)

#: Statuses a caller may assign through PATCH.
PATCHABLE_STATUSES: frozenset[MemoryStatus] = frozenset(
    {
        MemoryStatus.ACTIVE,
        MemoryStatus.HISTORICAL,
        MemoryStatus.CONFLICTING,
        MemoryStatus.ARCHIVED,
    }
)


class EntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    entity_type: EntityType = EntityType.OTHER
    aliases: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if v.strip()]
        return list(dict.fromkeys(cleaned))


class RelationshipInput(BaseModel):
    """An asserted edge between two entities named in the same memory.

    Endpoints are given by name and must also appear in the memory's `entities` list.
    Requiring that explicitly avoids creating entities as a side effect of a
    relationship, which would make a typo silently spawn a new node.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=200)
    predicate: str = Field(pattern=PREDICATE_PATTERN)
    target: str = Field(min_length=1, max_length=200)
    confidence: Confidence = 1.0
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    reference: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=500)


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=10_000)
    memory_type: MemoryType = MemoryType.SEMANTIC
    status: MemoryStatus = MemoryStatus.ACTIVE
    confidence: Confidence = 1.0
    entities: list[EntityInput] = Field(default_factory=list, max_length=50)
    relationships: list[RelationshipInput] = Field(default_factory=list, max_length=100)
    source: SourceInput | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    #: Memories this one replaces. They are marked `superseded` and their validity
    #: windows are closed, but they are never deleted.
    supersedes: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: MemoryStatus) -> MemoryStatus:
        if value not in CREATABLE_STATUSES:
            allowed = ", ".join(sorted(s.value for s in CREATABLE_STATUSES))
            raise ValueError(
                f"status {value.value!r} cannot be set when creating a memory "
                f"(allowed: {allowed})"
            )
        return value

    @field_validator("supersedes")
    @classmethod
    def _unique_supersedes(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if v.strip()]
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _check_content(self) -> MemoryCreate:
        if not self.content.strip():
            raise ValueError("content must contain at least one non-whitespace character")
        return self


class MemoryUpdate(BaseModel):
    """Metadata-only update.

    `content` is deliberately absent. Rewriting the text of a stored memory in place
    would destroy the historical record without leaving any trace that it changed;
    a changed fact is expressed by creating a new memory that `supersedes` this one.
    """

    model_config = ConfigDict(extra="forbid")

    memory_type: MemoryType | None = None
    status: MemoryStatus | None = None
    confidence: Confidence | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: MemoryStatus | None) -> MemoryStatus | None:
        if value is None:
            return value
        if value not in PATCHABLE_STATUSES:
            hint = {
                MemoryStatus.INVALID: "use POST /memories/{id}/invalidate",
                MemoryStatus.SUPERSEDED: (
                    "create the replacement memory with supersedes=[<this id>]"
                ),
            }.get(value, "")
            raise ValueError(
                f"status {value.value!r} cannot be set through an update"
                + (f"; {hint}" if hint else "")
            )
        return value

    def changed_fields(self) -> dict[str, object]:
        """Only the fields the caller actually sent."""
        return self.model_dump(exclude_unset=True)


class MemoryInvalidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


class MemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Memory]
    total: int
    limit: int
    offset: int


class EntityListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Entity]
    limit: int
    offset: int


class MemoryCreatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: MemoryDetail
