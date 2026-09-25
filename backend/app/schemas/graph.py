"""Response schemas for the memory graph exploration API (Phase 8,
docs/PHASE8_PLAN.md sections 7.5 and 9).

Every read returns a bounded, normalized, renderer-agnostic *slice* of the
user's canonical graph -- never the whole graph. A slice always states the
effective budget it was cut to and whether anything in scope was left out
(`truncated_*`, exact: computed by fetching one more than the budget). There
are no totals (counting the full neighbourhood would defeat the budget) and
no layout coordinates (layout is a UI concern).

Temporal meaning is computed here on the server with the same half-open
`[valid_from, valid_until)` semantics as `app.models.lifecycle`, so a client
never reimplements it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import DocumentChunkSummary, DocumentSummary
from app.models.domain import Source
from app.models.enums import EntityType, MemoryStatus, MemoryType, SourceType


class TemporalScope(StrEnum):
    """Which facts a slice includes (PHASE8_PLAN.md section 13).

    CURRENT  believed status (active/conflicting) AND window contains now
    AS_OF    any non-excluded status AND window contains the explicit `as_of`
    ALL      any non-excluded status, regardless of window; each element is
             labelled with its `temporal_state` relative to the slice's `as_of`
    """

    CURRENT = "current"
    AS_OF = "as_of"
    ALL = "all"


class TemporalState(StrEnum):
    """Where an element's validity window sits relative to an instant."""

    CURRENT = "current"
    HISTORICAL = "historical"  # window closed at or before the instant
    NOT_YET_VALID = "not_yet_valid"  # window starts after the instant


class NodeKind(StrEnum):
    ENTITY = "entity"
    MEMORY = "memory"
    SOURCE = "source"


class EdgeKind(StrEnum):
    RELATES_TO = "relates_to"
    MENTIONS = "mentions"
    SUPERSEDES = "supersedes"
    CONFLICTS_WITH = "conflicts_with"
    DUPLICATE_OF = "duplicate_of"
    DERIVED_FROM = "derived_from"


class GraphInclude(StrEnum):
    """Optional slice enrichments."""

    MEMORIES = "memories"  # memory nodes via MENTIONS, plus links among them
    MERGES = "merges"  # `merged_from_count` on entity nodes


#: Memory content is shown as a node label, never in full -- the full text is
#: one `GET /memories/{id}` (or the evidence endpoint) away.
LABEL_MAX_CHARS = 200


def temporal_state(
    valid_from: datetime | None, valid_until: datetime | None, at: datetime
) -> TemporalState:
    """Classify a validity window against `at`, half-open `[from, until)`.

    Mirrors `app.models.lifecycle.is_valid_at` (and the Cypher CASE in
    `app.repositories.graph_repository`, which a test pins against this):
    a null `valid_from` means "since the beginning", a null `valid_until`
    means "no known end".
    """
    if valid_from is not None and at < valid_from:
        return TemporalState.NOT_YET_VALID
    if valid_until is not None and at >= valid_until:
        return TemporalState.HISTORICAL
    return TemporalState.CURRENT


class GraphBudget(BaseModel):
    """The effective, post-clamp bounds a slice was cut to."""

    model_config = ConfigDict(extra="forbid")

    max_nodes: int
    #: None only for the memory timeline, whose edges are structurally bounded
    #: by its (bounded) node set.
    max_edges: int | None = None
    max_hops: int | None = None
    max_memories_per_entity: int | None = None


class GraphSliceNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    #: Entity name, truncated memory content, or source type/reference.
    label: str
    entity_type: EntityType | None = None
    memory_type: MemoryType | None = None
    source_type: SourceType | None = None
    status: MemoryStatus | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    temporal_state: TemporalState | None = None
    created_at: datetime | None = None
    #: BFS distance from the slice root (entity nodes of a neighborhood only).
    hops: int | None = None
    aliases: list[str] | None = None
    #: Entities merged directly into this one (`include=merges` only).
    merged_from_count: int | None = None


class GraphSliceEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Stable and deterministic. For `relates_to` it identifies the aggregate
    #: `(source, predicate, target, status, temporal_state)`, not one stored edge.
    id: str
    kind: EdgeKind
    source: str
    target: str
    predicate: str | None = None
    status: MemoryStatus | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    temporal_state: TemporalState | None = None
    #: `relates_to` only: the memories that asserted the aggregated edges
    #: (ADR 0001), bounded to `RELATES_TO_MEMORY_IDS_CAP`; the full list is the
    #: evidence endpoint's job.
    memory_ids: list[str] | None = None
    #: `relates_to` only: how many stored RELATES_TO edges were aggregated.
    assertion_count: int | None = None


class GraphSlice(BaseModel):
    """A bounded, read-only projection of the user's canonical graph."""

    model_config = ConfigDict(extra="forbid")

    #: Root node id (the canonical id if a redirect occurred); None for overview.
    root: str | None = None
    #: The id that was asked for, when a MERGED_INTO redirect was followed.
    resolved_from: str | None = None
    redirect_depth: int = 0
    temporal_scope: TemporalScope
    #: The instant temporal classification was computed at (now, unless an
    #: explicit `as_of` was requested).
    as_of: datetime
    budget: GraphBudget
    #: Exact: True iff at least one more in-scope node existed beyond the budget.
    truncated_nodes: bool = False
    #: Exact: True iff at least one more in-scope edge existed beyond the budget.
    truncated_edges: bool = False
    #: True iff memory nodes were left out by the per-entity memory cap or the
    #: node budget (`include=memories` only).
    truncated_memories: bool = False
    nodes: list[GraphSliceNode] = Field(default_factory=list)
    edges: list[GraphSliceEdge] = Field(default_factory=list)


class EvidenceStatus(StrEnum):
    """Why an assertion does or does not carry resolvable evidence. Evidence is
    reported as unavailable explicitly, never fabricated."""

    AVAILABLE = "available"  # memory found and at least one source resolved
    NO_ASSERTING_MEMORY = "no_asserting_memory"  # edge has no memory_id
    MEMORY_NOT_FOUND = "memory_not_found"  # memory_id points at nothing readable
    MEMORY_EXCLUDED = "memory_excluded"  # asserting memory is merged/invalid/archived
    NO_SOURCES = "no_sources"  # memory exists but has no DERIVED_FROM source
    SOURCES_UNRESOLVED = "sources_unresolved"  # sources exist, none resolvable


class EvidenceSource(BaseModel):
    """One source of an asserting memory, resolved through the existing
    provenance semantics (message, or document + chunk). `resolved` is False
    when the source's referent could not be found -- it degrades to source
    metadata, exactly as `GET /memories/{id}/provenance` does."""

    model_config = ConfigDict(extra="forbid")

    source: Source
    resolved: bool
    message_id: str | None = None
    message_role: str | None = None
    #: Length-bounded to `EVIDENCE_TEXT_MAX_CHARS`.
    message_content: str | None = None
    document: DocumentSummary | None = None
    document_chunk: DocumentChunkSummary | None = None


class EvidenceMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    content: str
    memory_type: MemoryType
    status: MemoryStatus
    confidence: float
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    temporal_state: TemporalState
    created_at: datetime
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    duplicate_of: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    sources: list[EvidenceSource] = Field(default_factory=list)


class EvidenceAssertion(BaseModel):
    """One stored RELATES_TO edge and the memory that asserted it."""

    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    status: MemoryStatus
    confidence: float
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    temporal_state: TemporalState
    memory_id: str | None = None
    evidence_status: EvidenceStatus
    memory: EvidenceMemory | None = None


class EdgeEvidence(BaseModel):
    """`GET /graph/edges/evidence`: relationship -> memories -> provenance in
    one bounded response (PHASE8_PLAN.md section 9.3)."""

    model_config = ConfigDict(extra="forbid")

    source_entity_id: str
    target_entity_id: str
    predicate: str
    #: Original ids asked for, when a MERGED_INTO redirect was followed.
    source_resolved_from: str | None = None
    target_resolved_from: str | None = None
    temporal_scope: TemporalScope
    as_of: datetime
    limit: int
    #: Exact: True iff more matching RELATES_TO edges existed beyond `limit`.
    truncated: bool = False
    #: True iff at least one assertion has `evidence_status=available`.
    evidence_available: bool
    assertions: list[EvidenceAssertion] = Field(default_factory=list)
    #: Memory and source nodes plus `derived_from` edges, so a client can draw
    #: Memory -> Source without reshaping `assertions`.
    nodes: list[GraphSliceNode] = Field(default_factory=list)
    edges: list[GraphSliceEdge] = Field(default_factory=list)
