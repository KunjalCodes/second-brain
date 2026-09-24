"""Controlled vocabularies for the memory graph.

Values are lowercase strings and are persisted verbatim as node/relationship
properties, so renaming a member is a data migration.
"""

from __future__ import annotations

from enum import StrEnum


class MemoryStatus(StrEnum):
    """Lifecycle state of a memory (CLAUDE.md section 11).

    ACTIVE       currently believed true
    HISTORICAL   was true and its validity window has closed, but nothing replaced it
    SUPERSEDED   explicitly replaced by a newer memory (see SUPERSEDES edge) --
                 a TEMPORAL claim: this fact changed over time (ADR 0003)
    INVALID      retracted; treated as never having been true
    CONFLICTING  contradicts another memory; unresolved and needs review
    ARCHIVED     retained for the record but excluded from all retrieval
    MERGED       a human reviewer confirmed this memory duplicates another (see
                 the DUPLICATE_OF edge) -- an IDENTITY/consolidation claim, not a
                 temporal one (ADR 0010, PHASE4_PLAN.md section 8). Deliberately
                 distinct from SUPERSEDED: two MERGED memories can share the
                 exact same validity window, extracted from different messages.
                 Never used as a substitute for invalidation or supersession.
    """

    ACTIVE = "active"
    HISTORICAL = "historical"
    SUPERSEDED = "superseded"
    INVALID = "invalid"
    CONFLICTING = "conflicting"
    ARCHIVED = "archived"
    MERGED = "merged"


#: Statuses that represent knowledge the system currently stands behind.
BELIEVED_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.ACTIVE, MemoryStatus.CONFLICTING}
)

#: Statuses excluded from ordinary retrieval unless explicitly requested.
#: MERGED joins INVALID/ARCHIVED here (Phase 4, ADR 0010): a merged memory is
#: still fully resolvable by direct id (GET /memories/{id} shows duplicate_of),
#: it just doesn't compete for search/retrieval attention with its canonical.
EXCLUDED_FROM_DEFAULT_RETRIEVAL: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.INVALID, MemoryStatus.ARCHIVED, MemoryStatus.MERGED}
)


class MemoryType(StrEnum):
    """Memory taxonomy (CLAUDE.md section 10)."""

    EPISODIC = "episodic"      # what happened
    SEMANTIC = "semantic"      # what is known
    PROCEDURAL = "procedural"  # how something is done
    WORKING = "working"        # temporary task context


class EntityType(StrEnum):
    """Entity taxonomy (CLAUDE.md section 7)."""

    PERSON = "person"
    PROJECT = "project"
    COMPANY = "company"
    TECHNOLOGY = "technology"
    CONCEPT = "concept"
    DOCUMENT = "document"
    DECISION = "decision"
    EVENT = "event"
    TASK = "task"
    OTHER = "other"


class SourceType(StrEnum):
    """Provenance kinds (CLAUDE.md section 16)."""

    CONVERSATION = "conversation"
    MESSAGE = "message"
    DOCUMENT = "document"
    URL = "url"
    TOOL_RESULT = "tool_result"
    USER_STATEMENT = "user_statement"
    SYSTEM_INFERENCE = "system_inference"


class Direction(StrEnum):
    """Traversal direction for relationship queries."""

    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


#: Reference vocabulary from CLAUDE.md section 8. Predicates are NOT restricted to
#: this set -- extraction in later phases must be free to produce specific predicates --
#: but these are the ones the seed data and docs use.
KNOWN_PREDICATES: frozenset[str] = frozenset(
    {
        "works_on", "uses", "created_by", "depends_on", "learned",
        "related_to", "replaced", "supersedes", "causes", "belongs_to",
    }
)
