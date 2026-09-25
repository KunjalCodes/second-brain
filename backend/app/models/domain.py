"""Domain models.

These are the canonical in-process representation of what lives in the graph. They
double as API response models -- there is no separate DTO layer, because in Phase 1 a
mapping layer would add indirection without adding behaviour.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EntityType, MemoryStatus, MemoryType, SourceType


class Entity(BaseModel):
    """An identifiable object in the user's knowledge (CLAUDE.md section 7)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    name: str
    normalized_name: str
    entity_type: EntityType
    aliases: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class Source(BaseModel):
    """Where a memory came from (CLAUDE.md section 16)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    source_type: SourceType
    reference: str | None = None
    location: str | None = None
    created_at: datetime


class Relationship(BaseModel):
    """A temporally-scoped edge between two entities.

    `memory_id` is the memory that asserted this edge, which is what makes every
    edge traceable back to its provenance.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    predicate: str
    source_entity_id: str
    target_entity_id: str
    status: MemoryStatus
    confidence: float
    memory_id: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Memory(BaseModel):
    """A unit of information that should persist (CLAUDE.md section 9)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    content: str
    memory_type: MemoryType
    status: MemoryStatus
    confidence: float
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    invalidation_reason: str | None = None


class RelationshipView(BaseModel):
    """A relationship enriched with its endpoint entities, for API responses."""

    model_config = ConfigDict(extra="forbid")

    relationship: Relationship
    source_entity: Entity
    target_entity: Entity


class MemoryDetail(BaseModel):
    """A memory together with everything reachable from it in one hop.

    `sources` is many-to-many (Phase 2): a candidate the decision engine classifies
    as DUPLICATE attaches an additional Source to an existing memory rather than
    creating a new one, so a corroborated memory can have more than one source --
    see `MemoryRepository.attach_source` and docs/PHASE2_PLAN.md section 4.
    """

    model_config = ConfigDict(extra="forbid")

    memory: Memory
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)
    #: Phase 2: memories flagged CONFLICTING against this one. Neither memory is
    #: invalidated or superseded by this relationship -- both remain visible, and
    #: the conflict is left for later, better-evidenced resolution
    #: (docs/adr/0007).
    conflicts_with: list[str] = Field(default_factory=list)
    #: Phase 4 (ADR 0010): memories this one was confirmed a duplicate of --
    #: outgoing DUPLICATE_OF. Never destroyed on confirmation; this memory keeps
    #: existing (status MERGED) and remains directly resolvable by id.
    duplicate_of: list[str] = Field(default_factory=list)
    #: The reverse: memories confirmed duplicates OF this one (this memory is
    #: the canonical representative) -- incoming DUPLICATE_OF.
    duplicates: list[str] = Field(default_factory=list)


class EntityDetail(BaseModel):
    """An entity with summary counts."""

    model_config = ConfigDict(extra="forbid")

    entity: Entity
    memory_count: int = 0
    relationship_count: int = 0


class EntityStateView(BaseModel):
    """Current vs historical view of an entity's relationships.

    This is the derived projection that PRODUCT.md section 9 describes as
    `currently_uses` / `previously_used`. It is computed from one predicate plus its
    validity window rather than stored as two different predicates -- see
    docs/adr/0003-relationship-representation.md.
    """

    model_config = ConfigDict(extra="forbid")

    entity: Entity
    as_of: datetime
    current: list[RelationshipView] = Field(default_factory=list)
    historical: list[RelationshipView] = Field(default_factory=list)


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: Entity
    hops: int


class Neighborhood(BaseModel):
    """An N-hop subgraph around a root entity."""

    model_config = ConfigDict(extra="forbid")

    root: Entity
    hops: int
    nodes: list[GraphNode] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    #: Phase 8 (D1): True iff GRAPH_MAX_NODES or GRAPH_MAX_EDGES cut the
    #: result (exact: one more than the cap is fetched). Additive; clients that
    #: predate it can ignore it.
    truncated: bool = False


class GraphPathStep(BaseModel):
    """One hop of a real, Neo4j-traversed path -- never an LLM-generated
    explanation (PHASE4_PLAN.md section 4). `predicate`/`direction` are `None`
    only for the root step (the starting entity itself, which was reached by
    no relationship).
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_name: str
    predicate: str | None = None
    direction: str | None = None  # "outgoing" | "incoming"


class GraphNodeWithPath(BaseModel):
    """A neighbourhood node plus the one shortest path that reached it --
    `GraphNode` extended with path capture, kept as a separate type (rather
    than adding an optional field to `GraphNode`) so the existing,
    already-tested interactive neighbourhood endpoint's return shape is
    completely unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    entity: Entity
    hops: int
    path: list[GraphPathStep] = Field(default_factory=list)


class ResolvedEntity(BaseModel):
    """The result of following MERGED_INTO redirects to a canonical entity
    (ADR 0010, PHASE4_PLAN.md section 9) -- the ONE canonical resolution
    mechanism every entity-retrieval path uses, never a scattered
    `if entity.merged_into` check.

    `original_id` is preserved even when a redirect happened, for audit/
    provenance -- callers that need to know "what id was actually asked for"
    (as opposed to what was returned) always have it.
    """

    model_config = ConfigDict(extra="forbid")

    entity: Entity
    original_id: str
    redirect_depth: int  # 0 if no redirect followed
    was_redirected: bool
