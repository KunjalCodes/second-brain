"""Build domain models from raw graph property maps.

Neo4j does not store null-valued properties, so a map projection simply omits them;
every optional field is therefore read with `.get()`.
"""

from __future__ import annotations

from typing import Any

from app.models.domain import Entity, Memory, Relationship, Source


def to_memory(row: dict[str, Any]) -> Memory:
    return Memory(
        id=row["id"],
        user_id=row["user_id"],
        content=row["content"],
        memory_type=row["memory_type"],
        status=row["status"],
        confidence=row["confidence"],
        valid_from=row.get("valid_from"),
        valid_until=row.get("valid_until"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        invalidation_reason=row.get("invalidation_reason"),
    )


def to_entity(row: dict[str, Any]) -> Entity:
    return Entity(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        normalized_name=row["normalized_name"],
        entity_type=row["entity_type"],
        aliases=list(row.get("aliases") or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def to_relationship(row: dict[str, Any]) -> Relationship:
    return Relationship(
        id=row["id"],
        user_id=row["user_id"],
        predicate=row["predicate"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        status=row["status"],
        confidence=row["confidence"],
        memory_id=row.get("memory_id"),
        valid_from=row.get("valid_from"),
        valid_until=row.get("valid_until"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def to_source(row: dict[str, Any]) -> Source:
    return Source(
        id=row["id"],
        user_id=row["user_id"],
        source_type=row["source_type"],
        reference=row.get("reference"),
        location=row.get("location"),
        created_at=row["created_at"],
    )
