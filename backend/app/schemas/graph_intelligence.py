"""Response schemas for the Phase 15 graph-intelligence endpoints.

Every element is either a stored record (memory, entity, source) or a stored
relationship aggregated over its assertions. Anything that is *derived* --
a multi-hop connection, a relevance score, a "why" sentence -- is labelled as
derived (`basis`, `signals`), never presented as something the user said.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain import Entity
from app.models.enums import EntityType, MemoryStatus, MemoryType, SourceType
from app.schemas.graph import GraphSliceEdge, GraphSliceNode, TemporalScope, TemporalState


class RelationKind(StrEnum):
    """What a predicate asserts (Phase 15, M7). Decided by the predicate the
    memory STATED -- never inferred from timing or co-occurrence."""

    CAUSAL = "causal"  # led_to, caused, fixed_by, ... -- stated as a cause
    POSSIBLE_CAUSE = "possible_cause"  # stated as uncertain
    ASSOCIATION = "association"  # everything else


class EvidenceBasis(StrEnum):
    #: Asserted by at least one stored memory.
    DIRECTLY_STATED = "directly_stated"
    #: Asserted by more than one stored memory.
    SUPPORTED_BY_MULTIPLE_MEMORIES = "supported_by_multiple_memories"
    #: A connection made by chaining stated relationships through other
    #: entities. Each step is stated; the connection itself is not.
    INFERRED_FROM_GRAPH = "inferred_from_graph"
    #: Included only because of the requested time scope (as_of / historical /
    #: all): not true now.
    TEMPORALLY_DERIVED = "temporally_derived"


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    reference: str | None
    location: str | None
    #: As `SourceProvenance.resolved` (Phase 14).
    resolved: bool | None


class StepRelationship(BaseModel):
    """One stored relationship between two adjacent path entities."""

    model_config = ConfigDict(extra="forbid")

    predicate: str
    #: `forward`: stored from the path's earlier entity to the later one.
    direction: str
    relation_kind: RelationKind
    status: MemoryStatus
    temporal_state: TemporalState
    valid_from: datetime | None
    valid_until: datetime | None
    assertion_count: int
    confidence: float | None
    memory_ids: list[str]
    basis: list[EvidenceBasis]


class PathStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_entity_id: str
    from_name: str
    to_entity_id: str
    to_name: str
    relationships: list[StepRelationship]


class GraphPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_ids: list[str]
    length: int
    steps: list[PathStep]
    basis: EvidenceBasis
    #: Heuristic, explainable ranking inputs (docs/PHASE15_GRAPH_INTELLIGENCE.md).
    signals: dict[str, float]
    score: float


class PathsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_entity_id: str
    target_entity_id: str
    source_resolved_from: str | None
    target_resolved_from: str | None
    temporal_scope: TemporalScope
    as_of: datetime
    max_hops: int
    max_paths: int
    paths: list[GraphPath]
    #: Why `paths` is empty: `same_entity` | `not_connected_within_hops` |
    #: `node_budget_exhausted`. None when paths were found.
    reason: str | None
    explored_nodes: int
    truncated: bool


class SupportingMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    status: MemoryStatus
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime
    superseded_by: list[str]
    conflicts_with: list[str]
    sources: list[SourceRef]


class EdgeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_entity_id: str
    target_entity_id: str
    predicate: str
    relation_kind: RelationKind
    basis: list[EvidenceBasis]
    temporal_scope: TemporalScope
    as_of: datetime
    temporal_state: TemporalState
    status: MemoryStatus
    valid_from: datetime | None
    valid_until: datetime | None
    confidence: float | None
    assertion_count: int
    #: A sentence built only from the stored windows/statuses/links above.
    why: str
    supporting_memories: list[SupportingMemory]
    truncated: bool


class ContextRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    other_entity_id: str
    other_name: str
    other_entity_type: EntityType
    #: `outgoing`: this entity is the stored source of the relationship.
    direction: str
    predicate: str
    relation_kind: RelationKind
    status: MemoryStatus
    temporal_state: TemporalState
    valid_from: datetime | None
    valid_until: datetime | None
    assertion_count: int
    confidence: float | None
    memory_ids: list[str]


class RelatedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    name: str
    entity_type: EntityType
    predicates: list[str]
    signals: dict[str, float]
    score: float


class ContextMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    memory_type: MemoryType
    status: MemoryStatus
    temporal_state: TemporalState
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime
    source_types: list[str]


class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    #: `began` (valid_from) | `ended` (valid_until).
    kind: str
    memory_id: str
    content: str
    status: MemoryStatus


class MergedEntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    name: str


class EntityContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: Entity
    resolved_from: str | None
    merged_from: list[MergedEntityRef]
    as_of: datetime
    current_relationships: list[ContextRelationship]
    historical_relationships: list[ContextRelationship]
    related_entities: list[RelatedEntity]
    memories: list[ContextMemory]
    memory_total: int
    conflicting_memory_total: int
    timeline: list[TimelineEvent]
    #: Among the listed memories only.
    source_type_counts: dict[str, int]
    truncated_relationships: bool
    truncated_memories: bool


class EvolutionKind(StrEnum):
    NEW_FACT = "new_fact"
    SUPERSESSION = "supersession"
    CONFLICT = "conflict"
    CONFLICT_RESOLVED = "conflict_resolved"
    INVALIDATION = "invalidation"
    MERGE = "merge"
    RECONFIRMATION = "reconfirmation"


class EvolutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvolutionKind
    at: datetime
    memory_id: str
    content: str
    status: MemoryStatus
    #: Stored ids this event refers to (the superseded memory, the canonical
    #: duplicate, the conflicting memory, ...).
    related_memory_ids: list[str]
    detail: str
    sources: list[SourceRef]


class MemoryEvolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    events: list[EvolutionEvent]
    #: Chain members believed and valid now.
    current_memory_ids: list[str]
    truncated: bool


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    temporal_scope: TemporalScope | None = None
    as_of: datetime | None = None
    max_nodes: int | None = Field(default=None, ge=1)
    max_evidence: int | None = Field(default=None, ge=1)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    status: MemoryStatus
    temporal_state: str
    retrieved_via: list[str]
    #: The hybrid retrieval signals (heuristic; see docs/PHASE3_PLAN.md §8).
    signals: dict[str, float]
    score: float
    sources: list[SourceRef]


class ContextSubgraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temporal_scope: TemporalScope | None
    #: The temporal intent retrieval applied (explicit scope, or detected).
    temporal_intent: str
    as_of: datetime
    anchors: list[str]
    nodes: list[GraphSliceNode]
    edges: list[GraphSliceEdge]
    #: Per node / edge id: the heuristic relevance inputs and score.
    node_signals: dict[str, dict[str, float]]
    edge_signals: dict[str, dict[str, float]]
    evidence: list[EvidenceItem]
    paths: list[GraphPath]
    #: How the subgraph was built, as counts (candidates -> kept).
    construction: dict[str, int]
    max_nodes: int
    truncated: bool
