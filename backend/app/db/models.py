"""PostgreSQL tables: application, conversation and extraction state.

**None of these tables is canonical memory.** The canonical persistent AI memory
graph lives in Neo4j (`Memory`, `Entity`, `Source`, relationships, temporal state)
and is never duplicated, mirrored or cached here. See
docs/adr/0006-postgresql-for-application-state.md.

The only references from here into the graph are opaque id strings
(`ExtractionCandidate.memory_id`, `EntityMergeCandidate.entity_id_*`) used for
provenance display and idempotency -- never copies of memory state.

Only portable SQL is used, so the same migrations run on a local PostgreSQL,
Supabase or Neon with nothing changing but DATABASE_URL.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

#: Length cap on the user id, mirroring app.api.deps.MAX_USER_ID_LENGTH.
USER_ID_LENGTH = 128

#: The pgvector column's declared width (ADR 0009, PHASE3_PLAN.md section 12).
#: This is a SCHEMA property -- fixed by the migration that created the column --
#: not a runtime setting. It must match `Settings.embedding_dimension`'s default;
#: changing it requires a new migration, not just a changed environment variable.
#: `Database.connect` asserts the two agree at startup so a mismatch fails loudly
#: rather than corrupting queries silently.
EMBEDDING_DIMENSION = 768


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    """Identity *reference* table -- deliberately minimal.

    **This is not an authentication system.** It stores no passwords, no API keys,
    no credentials, no sessions and no sensitive profile data. Authentication is
    Phase 9; until then the user id still arrives via the `X-User-Id` development
    header (docs/adr/0005).

    Its purpose is referential integrity and a consistent isolation key between the
    two stores:

        PostgreSQL users.id  <-->  Neo4j user_id namespace property

    `external_auth_id` is the empty seam where a real identity provider's subject
    will attach in Phase 9.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(USER_ID_LENGTH), primary_key=True)
    external_auth_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("conversations_user_recent", "user_id", "last_message_at"),
    )


class Message(Base):
    """One conversational turn. Immutable once written.

    `sequence` is monotonic within a conversation and is what makes ordering
    deterministic -- timestamps can collide at the same microsecond, so they are
    never used as the sole ordering key.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Extraction bookkeeping. `extraction_gate_reason` records why the gate skipped
    # a message, which is what makes the gate's own precision measurable.
    extraction_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    extraction_gate_reason: Mapped[str | None] = mapped_column(String(200))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="messages_conversation_sequence"),
        CheckConstraint(
            "role IN ('user','assistant','system')", name="messages_role_valid"
        ),
        CheckConstraint(
            "extraction_status IN "
            "('pending','queued','in_progress','done','failed','skipped')",
            name="messages_extraction_status_valid",
        ),
        Index("messages_conversation_order", "conversation_id", "sequence"),
        Index("messages_user_recent", "user_id", "created_at"),
        Index(
            "messages_extraction_todo",
            "extraction_status",
            postgresql_where=text(
                "extraction_status IN ('pending','failed','skipped')"
            ),
        ),
    )


class ExtractionJob(Base):
    """One unit of extraction work over a *window* of messages OR document
    chunks (Phase 6, ADR 0012, PHASE6_PLAN.md section 3).

    `source_kind` discriminates which of the two mutually-exclusive target
    pairs is populated: `('message', conversation_id, target_message_id)` or
    `('document_chunk', document_id, target_chunk_id)`. Every row written
    before Phase 6 has `source_kind='message'` (the migration's default),
    `document_id`/`target_chunk_id` NULL -- this table's pre-Phase-6 meaning
    is completely unchanged for every existing row.

    `window_start_sequence`/`window_end_sequence` are reused unchanged for
    document jobs: a chunk's `ordinal` plays the exact role a message's
    `sequence` plays today, so "window" becomes "a range of adjacent chunk
    ordinals" with zero new columns.

    Two separate unique constraints (`extraction_jobs_idempotency` on
    `target_message_id`, `extraction_jobs_chunk_idempotency` on
    `target_chunk_id`) rather than one covering both target columns --
    PostgreSQL treats NULL as distinct from NULL in a unique constraint, so
    each constraint is inert for the source kind it doesn't apply to (every
    document job's `target_message_id` is NULL and never collides with
    another NULL), giving each source kind its own real idempotency
    protection without a single constraint needing to express an XOR.

    `locked_at`/`locked_by` form a lease. Claiming uses
    `SELECT ... FOR UPDATE SKIP LOCKED`, giving at-least-once delivery that is safe
    under concurrency; a worker that dies leaves an expired lease for the sweep to
    reclaim.
    """

    __tablename__ = "extraction_jobs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="message"
    )
    conversation_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Window: inclusive sequence/ordinal range; target is the newest
    # message/chunk in it.
    target_message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("messages.id"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    target_chunk_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("document_chunks.id"), nullable=True
    )
    window_start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    window_end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    extractor_version: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)

    # Observability and cost (EVALUATION.md 13 and 21). No secrets are ever stored.
    provider: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    candidate_count: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidates: Mapped[list[ExtractionCandidate]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "target_message_id", "extractor_version", name="extraction_jobs_idempotency"
        ),
        UniqueConstraint(
            "target_chunk_id", "extractor_version", name="extraction_jobs_chunk_idempotency"
        ),
        CheckConstraint(
            "status IN ('pending','in_progress','done','failed','abandoned')",
            name="extraction_jobs_status_valid",
        ),
        CheckConstraint(
            "window_end_sequence >= window_start_sequence",
            name="extraction_jobs_window_ordered",
        ),
        CheckConstraint(
            "source_kind IN ('message','document_chunk')",
            name="extraction_jobs_source_kind_valid",
        ),
        CheckConstraint(
            "(source_kind = 'message' AND conversation_id IS NOT NULL "
            " AND target_message_id IS NOT NULL AND document_id IS NULL "
            " AND target_chunk_id IS NULL)"
            " OR "
            "(source_kind = 'document_chunk' AND document_id IS NOT NULL "
            " AND target_chunk_id IS NOT NULL AND conversation_id IS NULL "
            " AND target_message_id IS NULL)",
            name="extraction_jobs_source_kind_consistent",
        ),
        Index("extraction_jobs_claimable", "status", "next_attempt_at"),
        Index("extraction_jobs_lease", "status", "locked_at"),
        Index("extraction_jobs_user", "user_id", "created_at"),
    )


class ExtractionCandidate(Base):
    """The audit boundary: what the LLM proposed, and what the engine decided.

        LLM proposed -> engine evaluated -> engine decided -> Neo4j persisted

    **This is NOT canonical memory.** It stores the *proposal* verbatim plus the
    decision taken, not the resulting memory's state. `memory_id` is an opaque
    pointer into Neo4j used for provenance display and idempotency; nothing here
    duplicates the Memory model.

    The unique constraint on `(extraction_job_id, candidate_hash)` is the second
    idempotency layer: a job that wrote some memories and then crashed applies only
    the remainder on retry.
    """

    __tablename__ = "extraction_candidates"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    extraction_job_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("extraction_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    #: Exactly one of these two is set, matching the owning job's
    #: `source_kind` (Phase 6, ADR 0012) -- both nullable, neither was
    #: renamed, so every pre-Phase-6 row (source_message_id always set,
    #: source_chunk_id always NULL) keeps its exact original meaning.
    source_message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("messages.id"), nullable=True
    )
    source_chunk_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("document_chunks.id"), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    memory_id: Mapped[str | None] = mapped_column(String(64))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[ExtractionJob] = relationship(back_populates="candidates")

    __table_args__ = (
        UniqueConstraint(
            "extraction_job_id", "candidate_hash", name="extraction_candidates_idempotency"
        ),
        CheckConstraint(
            "decision IN ('NEW','DUPLICATE','UPDATE','SUPERSEDING',"
            "'CONFLICTING','INVALID','IGNORE','LIKELY_DUPLICATE','FLAGGED')",
            name="extraction_candidates_decision_valid",
        ),
        CheckConstraint(
            "(source_message_id IS NOT NULL AND source_chunk_id IS NULL)"
            " OR "
            "(source_message_id IS NULL AND source_chunk_id IS NOT NULL)",
            name="extraction_candidates_source_consistent",
        ),
        Index("extraction_candidates_user", "user_id", "created_at"),
        Index("extraction_candidates_memory", "memory_id"),
        Index("extraction_candidates_message", "source_message_id"),
        Index("extraction_candidates_chunk", "source_chunk_id"),
    )


class EntityMergeCandidate(Base):
    """A *possible* duplicate entity pair -- flagged, never merged automatically.

    A wrong merge destroys memory, while a missed merge is an inconvenience, so
    Phase 2 records the suspicion and the evidence and leaves both entities intact
    (docs/adr/0007).

    Phase 4 (ADR 0010) adds `version` for optimistic concurrency on review
    actions, and `reversed` to `status` -- the one genuinely new outcome
    (`merged` already covers "reviewed, confirmed"; `rejected` already covers
    "reviewed, not a match"; a previously-merged pair being un-merged is new).
    """

    __tablename__ = "entity_merge_candidates"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    entity_id_a: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id_b: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unreviewed"
    )
    #: Optimistic concurrency (ADR 0010, PHASE4_PLAN.md section 15): every review
    #: write requires the caller's `if_version` to match this, or the write is
    #: rejected with 409 rather than silently overwriting a newer decision.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "entity_id_a", "entity_id_b", name="entity_merge_candidates_pair"
        ),
        CheckConstraint(
            "status IN ('unreviewed','merged','rejected','reversed')",
            name="entity_merge_candidates_status_valid",
        ),
        CheckConstraint("entity_id_a < entity_id_b", name="entity_merge_candidates_ordered"),
    )


class MemoryEmbedding(Base):
    """A vector representation of one canonical Neo4j Memory's content.

    **NOT canonical memory** (ADR 0009, PHASE3_PLAN.md section 2). `memory_id` is
    an opaque pointer into Neo4j -- not a foreign key, since Neo4j is not a table
    this database can reference -- exactly the pattern already used by
    `ExtractionCandidate.memory_id` and `EntityMergeCandidate.entity_id_*`. This
    table stores only the vector and the metadata needed to know which model
    produced it; a memory's content, status and validity window are never copied
    here. Losing every row here loses retrieval convenience, never memory.

    One row per `(memory_id, embedding_provider, embedding_model)`, not one row
    per `memory_id`: a model change produces new rows alongside old ones rather
    than overwriting in place, so retrieval can always read a single, consistent
    model's rows and a migration is never a destructive, all-or-nothing cutover.
    """

    __tablename__ = "memory_embeddings"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    # Redundant with the column's declared width, kept for defense-in-depth
    # validation (PHASE3_PLAN.md section 12): a dimension mismatch is then a
    # clear application-level check, not an opaque pgvector error.
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "memory_id", "embedding_provider", "embedding_model",
            name="memory_embeddings_one_per_model",
        ),
        Index("memory_embeddings_user", "user_id"),
        Index("memory_embeddings_memory", "memory_id"),
        # The vector similarity index (HNSW, cosine) is created directly in the
        # Alembic migration rather than declared here: SQLAlchemy's ORM-level
        # Index construct does not model pgvector's `USING hnsw (... vector_cosine_ops)`
        # access-method syntax, and the migration is the authoritative, reviewable
        # record of the index's exact configuration (PHASE3_PLAN.md section 15).
    )


class MemoryReviewCandidate(Base):
    """A *possible* duplicate/flagged/conflicting memory pair -- flagged by the
    decision engine, never acted on automatically (ADR 0010, PHASE4_PLAN.md
    section 6).

    Closes a real Phase 3 gap: a `LIKELY_DUPLICATE`/`FLAGGED` decision was
    previously recorded only as unstructured text in
    `ExtractionCandidate.decision_reason`, with no queryable "pending review"
    list. `CONFLICTING` already has a real Neo4j `CONFLICTS_WITH` edge, but no
    PostgreSQL-side "has anyone looked at this" state -- this table is that
    workflow state, referencing Neo4j only by opaque id, exactly
    `ExtractionCandidate`'s own discipline. It never stores memory content.

    `memory_id_a` is the newer/candidate memory (the one just extracted);
    `memory_id_b` is the existing memory it was compared against. For
    `kind='conflicting'`, `memory_id_a`/`memory_id_b` are the two memories the
    Neo4j `CONFLICTS_WITH` edge already connects -- this row's job is only to
    track review status, never to duplicate that edge's existence.
    """

    __tablename__ = "memory_review_candidates"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    memory_id_a: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_id_b: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The extraction candidate that produced this flag -- the link that lets a
    #: reviewer trace "LLM proposed -> engine decided -> human corrected"
    #: without ever overwriting the original decision (PHASE4_PLAN.md section 11a).
    source_extraction_candidate_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("extraction_candidates.id"), nullable=True
    )
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unreviewed"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "memory_id_a", "memory_id_b", "kind",
            name="memory_review_candidates_pair",
        ),
        CheckConstraint(
            "kind IN ('likely_duplicate','flagged','conflicting')",
            name="memory_review_candidates_kind_valid",
        ),
        CheckConstraint(
            "status IN ('unreviewed','confirmed_duplicate','confirmed_distinct',"
            "'conflict_resolved')",
            name="memory_review_candidates_status_valid",
        ),
        CheckConstraint("memory_id_a <> memory_id_b", name="memory_review_candidates_distinct"),
        Index("memory_review_candidates_user_status", "user_id", "status"),
    )


class ReviewAuditLog(Base):
    """An insert-only record of every review decision (ADR 0010, PHASE4_PLAN.md
    section 11a).

    Distinguishes exactly the three-stage provenance the project's review model
    requires: "LLM proposed" is `ExtractionCandidate.payload`/`decision`
    (unchanged, never overwritten); "engine decided" is that same row's
    `decision`/`decision_reason`; "human corrected" is this table. Never
    updated after insert -- a correction to a correction is a new row, not an
    edit, matching ADR 0002's append-only principle applied to review state.
    This table is itself never memory: it is workflow metadata about who
    decided what and when, not a fact the memory graph represents.
    """

    __tablename__ = "review_audit_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    previous_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    resulting_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    #: = user_id today; a distinct reviewer identity from the acting user is
    #: Phase 9 (authentication) territory -- kept as its own column now so that
    #: change is additive later, not a rename.
    reviewer_id: Mapped[str] = mapped_column(String(USER_ID_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('CONFIRM_DUPLICATE','CONFIRM_DISTINCT','MERGE_ENTITY',"
            "'REJECT_ENTITY_MERGE','REVERSE_MERGE','RESOLVE_CONFLICT_SUPERSEDE',"
            "'RESOLVE_CONFLICT_KEEP_BOTH','RESOLVE_CONFLICT_REJECT')",
            name="review_audit_log_action_valid",
        ),
        CheckConstraint(
            "target_type IN ('memory_review_candidate','entity_merge_candidate')",
            name="review_audit_log_target_type_valid",
        ),
        Index("review_audit_log_user_created", "user_id", "created_at"),
        Index("review_audit_log_target", "target_type", "target_id"),
    )


class Document(Base):
    """One uploaded file (Phase 6, PHASE6_PLAN.md section 3).

    Never holds the file's bytes -- `storage_key` is an opaque pointer into
    `DocumentStorage` (local filesystem in v1, ADR 0013), the same discipline
    `ExtractionCandidate.memory_id` already uses for Neo4j. `filename` is
    display-only and is never used to construct a storage path or any
    filesystem location (section 9) -- `storage_key` is a generated id.
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: sha256 of the raw uploaded bytes -- whole-file dedup (section 8).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Opaque key into DocumentStorage -- never a path, never derived from
    #: `filename` (section 9/10).
    storage_key: Mapped[str] = mapped_column(String(100), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="uploaded"
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="documents_user_dedup"),
        CheckConstraint(
            "status IN ('uploaded','validating','parsing','chunking','chunked',"
            "'extracting','complete','partially_complete','failed')",
            name="documents_status_valid",
        ),
        Index("documents_user_status", "user_id", "status"),
    )


class DocumentChunk(Base):
    """One deterministic slice of a document's parsed text -- the document
    analogue of `Message` for the extraction pipeline (Phase 6, ADR 0012).

    `char_start`/`char_end` are offsets into the document's full parsed
    text -- the document-level analogue of `CandidateMemory.source_span`.
    `content` (the extracted text, never the original file bytes) lives here
    exactly like `Message.content` lives in PostgreSQL, needed for grounding
    and provenance display.
    """

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    heading_context: Mapped[str | None] = mapped_column(String(500))
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="document_chunks_ordinal"),
        CheckConstraint(
            "extraction_status IN "
            "('pending','queued','in_progress','done','failed','skipped')",
            name="document_chunks_extraction_status_valid",
        ),
        CheckConstraint("char_end > char_start", name="document_chunks_span_ordered"),
        Index("document_chunks_document_order", "document_id", "ordinal"),
        Index(
            "document_chunks_extraction_todo",
            "extraction_status",
            postgresql_where=text(
                "extraction_status IN ('pending','failed','skipped')"
            ),
        ),
    )


class IngestionJob(Base):
    """VALIDATING -> PARSING -> CHUNKING only -- the exact claim/lease/retry
    shape as `ExtractionJob`, one row per document (Phase 6, PHASE6_PLAN.md
    section 2/3). Extraction of the resulting chunks is a separate concern,
    tracked entirely by ordinary `ExtractionJob` rows (one per chunk).
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("document_id", name="ingestion_jobs_one_per_document"),
        CheckConstraint(
            "status IN ('pending','in_progress','done','failed','abandoned')",
            name="ingestion_jobs_status_valid",
        ),
        Index("ingestion_jobs_claimable", "status", "next_attempt_at"),
        Index("ingestion_jobs_lease", "status", "locked_at"),
    )


class EvaluationRun(Base):
    """One resumable evaluation-harness invocation (Phase 7,
    PHASE7_PLAN.md section 6/7).

    **Not user-owned data.** An evaluation run is operational/developer
    metadata about how well the system performs -- it has no `user_id`
    column at all (the synthetic `eval-*` users a run seeds in Neo4j/
    PostgreSQL while it executes are cleaned up by the harness itself, the
    same as every existing eval script already does; nothing about that
    cleanup changes here). See `tests/unit/test_sql_safety.py`'s exemption
    list for why `app/repositories/evaluation_repository.py` does not
    extend `UserScopedRepository`.

    Never a second source of truth for memory quality: `summary` holds only
    aggregate counts/scores (recall, precision, category breakdowns) --
    never raw memory or document content (Phase 7 security requirement).

    A run stays resumable (`status='pending'` or `'in_progress'`) until
    every one of its `EvaluationTaskResult` rows reaches `'completed'` or
    `'skipped'`, at which point `EvaluationRunTracker.finalize` (see
    `eval/persistence.py`) marks it `'completed'`. `'failed'` is reserved
    for the run itself crashing (e.g. a lost database connection) --
    distinct from any individual task's outcome, which lives on
    `EvaluationTaskResult` instead.
    """

    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    harness: Mapped[str] = mapped_column(String(50), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    llm_provider: Mapped[str | None] = mapped_column(String(50))
    llm_model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict | None] = mapped_column(JSONB)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    git_commit: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task_results: Mapped[list[EvaluationTaskResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('deterministic','real_llm')",
            name="evaluation_runs_mode_valid",
        ),
        CheckConstraint(
            "status IN ('pending','in_progress','completed','failed')",
            name="evaluation_runs_status_valid",
        ),
        Index(
            "evaluation_runs_lookup",
            "harness",
            "dataset_version",
            "mode",
            "created_at",
        ),
    )


class EvaluationTaskResult(Base):
    """One task's outcome within one `EvaluationRun` (Phase 7,
    PHASE7_PLAN.md section 6/7) -- the durable checkpoint a resumed run
    reads to decide which tasks it can skip.

    `status='completed'` means the task ran to completion without an
    infrastructure error or rate-limit -- `passed` (only meaningful then)
    is the separate, model-level judgment of whether the outcome was
    correct. This is deliberate: "which tasks failed because of the
    system" is `status='errored'`/`'rate_limited'`; "which tasks failed
    because of the LLM" is `status='completed'` with `passed=false`. Never
    conflated, per Phase 7's own stated Core Objective.

    `metrics` holds whatever the harness computed for this task (recall,
    precision, tool sequence, cited memory ids, latency, tokens) --
    whichever keys are actually available for that task; a key that was
    not computed is simply absent, never written as `0` or `null` standing
    in for "unknown" (Phase 7's explicit "use explicit N/A / unavailable
    semantics" requirement -- `eval/report.py`'s aggregation only averages
    over the tasks that actually have a given key).
    """

    __tablename__ = "evaluation_task_results"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    passed: Mapped[bool | None] = mapped_column()
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[EvaluationRun] = relationship(back_populates="task_results")

    __table_args__ = (
        UniqueConstraint("run_id", "task_id", name="evaluation_task_results_one_per_task"),
        CheckConstraint(
            "status IN ('pending','completed','rate_limited','errored','skipped')",
            name="evaluation_task_results_status_valid",
        ),
        Index("evaluation_task_results_run_status", "run_id", "status"),
    )


# -- Phase 9: authentication (docs/PHASE9_PLAN.md section 14, ADRs 0020/0021) --------
# AUTH identity lives here, separate from MEMORY OWNERSHIP identity (`users.id`,
# unchanged). No credential is ever stored in `users`.


class UserAccount(Base):
    """A login credential for exactly one ownership id (`users.id`). The
    server chooses `user_id` (UUID4 at registration, or an existing legacy id
    via the operator CLI) -- no client request ever carries it."""

    __tablename__ = "user_accounts"

    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    email_normalized: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    #: `scrypt$N$r$p$salt_b64$hash_b64` -- never a plaintext password.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="user_accounts_status_valid"),
        CheckConstraint(
            "email_normalized = lower(email_normalized)", name="user_accounts_email_lowercase"
        ),
    )


class AuthSession(Base):
    """A server-side session. Only the SHA-256 of the opaque bearer token is
    stored; the raw token exists only in the client's cookie/header."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("auth_sessions_user_active", "user_id", "revoked_at"),
        CheckConstraint("char_length(token_sha256) = 64", name="auth_sessions_token_hash_len"),
    )


class RateLimitCounter(Base):
    """One fixed-window counter. `bucket_key` is `<class>:<identity>` where the
    identity is `u:<user_id>`, `ip:<address>` or `email:<sha256>` -- never a
    raw email (ADR 0023)."""

    __tablename__ = "rate_limit_counters"

    bucket_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("rate_limit_counters_window", "window_start"),)


# -- Phase 16: projects (docs/PHASE16_ARCHITECTURE.md sections 3, 4, 9) ----------------
# A project is a MEMORY SPACE: it owns a dedicated `users` row (`space_id`) that every
# existing repository and Cypher statement already filters by, so isolation between
# projects holds by construction. `owner_user_id` is the authorising account.


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The memory-space id: a `users.id` that no account can log in as.
    space_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: SHA-256 hex of the normalised repository identity (never a filesystem path).
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: How the identity was derived: `remote_url` or `root_commits`.
    identity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "identity_key", name="projects_owner_identity"),
        UniqueConstraint("space_id", name="projects_space_unique"),
        Index("projects_owner", "owner_user_id", "created_at"),
        CheckConstraint("status IN ('active','archived')", name="projects_status_valid"),
        CheckConstraint(
            "identity_kind IN ('remote_url','root_commits')", name="projects_identity_kind_valid"
        ),
        CheckConstraint("char_length(identity_key) = 64", name="projects_identity_key_len"),
    )


class ProjectToken(Base):
    """A credential bound to exactly one project. Only the SHA-256 of the token
    is stored; the raw token is shown once, at creation."""

    __tablename__ = "project_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Subset of `read`, `ingest`, `propose`.
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("project_tokens_project", "project_id", "revoked_at"),
        CheckConstraint("char_length(token_sha256) = 64", name="project_tokens_hash_len"),
        CheckConstraint("cardinality(scopes) >= 1", name="project_tokens_scopes_nonempty"),
    )


# -- Phase 16: project history (docs/PHASE16_ARCHITECTURE.md sections 5-8) -------------
# These rows are scoped by `user_id` = the project's memory-space id, exactly like every
# other user-owned table, so `UserScopedRepository` and the SQL audit apply unchanged.
# `project_id` is carried as well, for referential integrity and per-project queries.


class AgentSession(Base):
    """A meaningful period of work by an external coding agent."""

    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: Chosen by the client (its own conversation/run id); unique per project.
    session_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Which agent/provider the CLIENT says it is. Informational, never an authority.
    agent_name: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    #: The agent-claimed time work began / ended (valid time), not the server's.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Explicit, from SESSION_ENDED: completed | blocked | abandoned.
    outcome: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        UniqueConstraint("user_id", "session_key", name="agent_sessions_key_unique"),
        Index("agent_sessions_recent", "user_id", "project_id", "last_event_at"),
        CheckConstraint("status IN ('active','ended')", name="agent_sessions_status_valid"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('completed','blocked','abandoned')",
            name="agent_sessions_outcome_valid",
        ),
    )


class AgentEvent(Base):
    """One recorded agent event: the AUDITABLE raw record. Canonical memory and
    graph facts are derived from it (projection), never the other way round."""

    __tablename__ = "agent_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    #: Server-assigned total order of RECORDING (never the client's clock).
    recorded_seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Idempotency key: the client's `event_id`, else a hash of the canonical event.
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Valid time as CLAIMED by the agent; `recorded_at` is when the server learned it.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Validated, normalised payload (schema per event type). Untrusted text.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Free-form label of the client integration (e.g. "claude-code"). Informational.
    source: Mapped[str | None] = mapped_column(String(80))
    projection_status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="pending"
    )
    memory_id: Mapped[str | None] = mapped_column(String(64))
    projection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    projection_error: Mapped[str | None] = mapped_column(String(300))
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("session_id", "event_key", name="agent_events_idempotency"),
        Index("agent_events_session_order", "user_id", "session_id", "recorded_seq"),
        Index("agent_events_project_recent", "user_id", "project_id", "recorded_seq"),
        Index(
            "agent_events_unprojected",
            "projection_status",
            "recorded_seq",
            postgresql_where=text("projection_status IN ('pending','failed')"),
        ),
        CheckConstraint(
            "event_type IN ('session_started','session_ended','user_request','decision',"
            "'file_inspected','file_modified','bug_discovered','error','fix','test_run',"
            "'commit','task_completed','task_blocked','memory_proposal')",
            name="agent_events_type_valid",
        ),
        CheckConstraint(
            "projection_status IN ('pending','projected','skipped','failed')",
            name="agent_events_projection_valid",
        ),
        CheckConstraint("char_length(payload_sha256) = 64", name="agent_events_hash_len"),
    )


class GitCommit(Base):
    """A commit imported by the local read-only collector. Structured metadata
    only: no diffs, no file contents."""

    __tablename__ = "git_commits"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    parents: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(200))
    #: SHA-256 of the lower-cased author email: identity without storing the address.
    author_email_sha256: Mapped[str | None] = mapped_column(String(64))
    authored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    body_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: Branch names the collector says contain this commit (bounded).
    branches: Mapped[list[str]] = mapped_column(ARRAY(String(255)), nullable=False)
    #: `[{path, status, additions, deletions, old_path?}]`, capped; the true count is
    #: `files_changed_count`, and `files_truncated` says the list is partial.
    files: Mapped[list] = mapped_column(JSONB, nullable=False)
    files_changed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    files_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    additions: Mapped[int | None] = mapped_column(Integer)
    deletions: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    projection_status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="pending"
    )
    memory_id: Mapped[str | None] = mapped_column(String(64))
    projection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    projection_error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("user_id", "sha", name="git_commits_sha_unique"),
        Index("git_commits_recent", "user_id", "project_id", "committed_at"),
        Index(
            "git_commits_unprojected",
            "projection_status",
            "committed_at",
            postgresql_where=text("projection_status IN ('pending','failed')"),
        ),
        CheckConstraint(
            "projection_status IN ('pending','projected','skipped','failed')",
            name="git_commits_projection_valid",
        ),
    )
