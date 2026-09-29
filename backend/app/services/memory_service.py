"""Memory lifecycle business logic.

All domain rules live here rather than in the route handlers or the repository:
handlers only translate HTTP, repositories only run Cypher. The service owns id
generation, entity normalization, validity-window validation and status transitions.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.domain import Memory, MemoryDetail
from app.models.enums import EXCLUDED_FROM_DEFAULT_RETRIEVAL, MemoryStatus, MemoryType, SourceType
from app.models.lifecycle import can_transition, validate_window
from app.models.normalization import normalize_name
from app.repositories.memory_repository import MemoryRepository
from app.schemas.memory import MemoryCreate, MemoryListResponse, MemoryUpdate

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryService:
    """Owns the memory lifecycle: create, read, update, invalidate, search."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._repo = repository
        # Injected so that tests can pin time and ids and assert exact stored values.
        self._now = now_fn
        self._new_id = id_fn

    # -- commands ----------------------------------------------------------

    async def create(
        self, *, user_id: str, payload: MemoryCreate, open_start: bool = False
    ) -> MemoryDetail:
        """Create a memory with its entities, relationships, source and supersessions.

        The whole write is one transaction (see MemoryRepository.create), and the
        result is read back from the graph rather than assembled optimistically, so a
        successful response reflects what is actually stored.
        """
        now = self._now()
        validate_window(payload.valid_from, payload.valid_until)

        memory_id = self._new_id()
        if memory_id in payload.supersedes:
            raise ValidationError("A memory cannot supersede itself")
        payload = await self._redirect_merged_entities(user_id, payload)

        entities, by_normalized = self._build_entities(payload)
        relationships = self._build_relationships(
            payload,
            by_normalized,
            user_id=user_id,
            memory_id=memory_id,
            now=now,
            open_start=open_start,
        )

        if payload.supersedes and not payload.relationships:
            # Not an error, but usually a mistake worth surfacing: superseding without
            # asserting a replacement leaves the entity with no current relationship.
            logger.warning(
                "Memory %s supersedes %d memories but asserts no relationships",
                memory_id,
                len(payload.supersedes),
            )

        memory_props: dict[str, Any] = {
            "id": memory_id,
            "user_id": user_id,
            "content": payload.content.strip(),
            "memory_type": payload.memory_type.value,
            "status": payload.status.value,
            "confidence": payload.confidence,
            "created_at": now,
            "updated_at": now,
            # A memory with no stated start is true from the moment it was recorded,
            # which keeps `as_of` queries total: every stored fact has a start.
            # `open_start` (Phase 14): an extracted past fact with a known end but
            # no known start ("used Heroku until May") -- stamping the recording
            # time as its start would put the start after the end.
            "valid_from": payload.valid_from or (None if open_start else now),
            "valid_until": payload.valid_until,
        }
        # Neo4j does not store null properties; drop them rather than writing nulls.
        memory_props = {k: v for k, v in memory_props.items() if v is not None}

        await self._repo.create(
            user_id=user_id,
            memory_props=memory_props,
            entities=entities,
            relationships=relationships,
            source=self._build_source(payload),
            supersedes=payload.supersedes,
            now=now,
            superseded_at=payload.valid_from or now,
        )

        stored = await self._repo.get(user_id=user_id, memory_id=memory_id)
        if stored is None:
            raise ConflictError(
                "The memory was written but could not be read back",
                details={"memory_id": memory_id},
            )
        return stored

    async def update(
        self, *, user_id: str, memory_id: str, payload: MemoryUpdate
    ) -> Memory:
        """Apply a metadata update, enforcing legal status transitions."""
        current = await self._require(user_id=user_id, memory_id=memory_id)
        changes = payload.changed_fields()
        if not changes:
            raise ValidationError("No updatable fields were provided")

        if payload.status is not None and not can_transition(
            current.memory.status, payload.status
        ):
            raise ConflictError(
                f"Cannot change status from {current.memory.status.value!r} to "
                f"{payload.status.value!r}",
                details={
                    "current_status": current.memory.status.value,
                    "requested_status": payload.status.value,
                },
            )

        # Validate the window that will exist *after* the update, not just the delta.
        new_from = (
            payload.valid_from if "valid_from" in changes else current.memory.valid_from
        )
        new_until = (
            payload.valid_until
            if "valid_until" in changes
            else current.memory.valid_until
        )
        validate_window(new_from, new_until)

        graph_changes = {
            key: (value.value if isinstance(value, StrEnum) else value)
            for key, value in changes.items()
        }

        updated = await self._repo.update(
            user_id=user_id,
            memory_id=memory_id,
            changes=graph_changes,
            now=self._now(),
        )
        if updated is None:
            raise NotFoundError("Memory not found", details={"memory_id": memory_id})
        return updated

    async def invalidate(
        self, *, user_id: str, memory_id: str, reason: str | None
    ) -> Memory:
        """Retract a memory. The node is kept; only its status and window change."""
        current = await self._require(user_id=user_id, memory_id=memory_id)
        if not can_transition(current.memory.status, MemoryStatus.INVALID):
            raise ConflictError(
                f"A memory with status {current.memory.status.value!r} cannot be "
                "invalidated",
                details={"current_status": current.memory.status.value},
            )
        invalidated = await self._repo.invalidate(
            user_id=user_id, memory_id=memory_id, reason=reason, now=self._now()
        )
        if invalidated is None:
            raise NotFoundError("Memory not found", details={"memory_id": memory_id})
        return invalidated

    async def attach_source(
        self,
        *,
        user_id: str,
        memory_id: str,
        source_type: SourceType,
        reference: str | None,
        location: str | None,
    ) -> MemoryDetail:
        """Corroborate an existing memory with an additional source.

        The extraction runner calls this for a DUPLICATE decision
        (docs/PHASE2_PLAN.md section 4): the memory's content, status and validity
        window are untouched -- only provenance grows.
        """
        await self._require(user_id=user_id, memory_id=memory_id)
        dedup_key = f"{reference or ''}||{location or ''}"
        await self._repo.attach_source(
            user_id=user_id,
            memory_id=memory_id,
            source={
                "id": self._new_id(),
                "source_type": source_type.value,
                "reference": reference,
                "location": location,
                "dedup_key": dedup_key,
            },
            now=self._now(),
        )
        return await self._require(user_id=user_id, memory_id=memory_id)

    async def link_conflicting(
        self, *, user_id: str, memory_id_a: str, memory_id_b: str
    ) -> None:
        """Flag two memories as contradicting each other (Phase 2 CONFLICTING).

        Neither memory is modified; this only records the suspicion for later
        review (docs/adr/0007's conservative policy).
        """
        await self._require(user_id=user_id, memory_id=memory_id_a)
        await self._require(user_id=user_id, memory_id=memory_id_b)
        await self._repo.link_conflicting(
            user_id=user_id,
            memory_id_a=memory_id_a,
            memory_id_b=memory_id_b,
            now=self._now(),
        )

    # -- queries -----------------------------------------------------------

    async def get(self, *, user_id: str, memory_id: str) -> MemoryDetail:
        return await self._require(user_id=user_id, memory_id=memory_id)

    async def search_by_terms(
        self, *, user_id: str, terms: list[str], limit: int
    ) -> list[tuple[str, str]]:
        return await self._repo.search_by_terms(user_id=user_id, terms=terms, limit=limit)

    async def search(
        self,
        *,
        user_id: str,
        query: str | None = None,
        statuses: list[MemoryStatus] | None = None,
        memory_types: list[MemoryType] | None = None,
        entity_name: str | None = None,
        as_of: datetime | None = None,
        include_excluded: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> MemoryListResponse:
        """Deterministic keyword + attribute search. No embeddings (that is Phase 3)."""
        effective_statuses = [s.value for s in statuses] if statuses else None
        if effective_statuses is None and not include_excluded:
            # Retracted and archived memories are hidden by default, but historical
            # and superseded ones stay visible: inspecting superseded memory is a core
            # product requirement (PRODUCT.md section 7).
            effective_statuses = [
                s.value for s in MemoryStatus if s not in EXCLUDED_FROM_DEFAULT_RETRIEVAL
            ]

        filters: dict[str, Any] = {
            "query": query,
            "statuses": effective_statuses,
            "memory_types": [t.value for t in memory_types] if memory_types else None,
            "as_of": as_of,
            "entity_normalized_name": (
                normalize_name(entity_name) if entity_name else None
            ),
        }
        items = await self._repo.search(
            user_id=user_id, filters=filters, limit=limit, offset=offset
        )
        total = await self._repo.count(user_id=user_id, filters=filters)
        return MemoryListResponse(items=items, total=total, limit=limit, offset=offset)

    # -- helpers -----------------------------------------------------------

    async def _require(self, *, user_id: str, memory_id: str) -> MemoryDetail:
        """Fetch a memory or raise NotFoundError.

        A memory belonging to a different user is reported as not-found rather than
        forbidden, so the API never confirms the existence of another user's records.
        """
        found = await self._repo.get(user_id=user_id, memory_id=memory_id)
        if found is None:
            raise NotFoundError("Memory not found", details={"memory_id": memory_id})
        return found

    async def _redirect_merged_entities(self, user_id: str, payload: MemoryCreate) -> MemoryCreate:
        """Point every entity (and relationship endpoint) that names a merged-away
        entity at its canonical entity; the name as written is kept as an alias
        (Phase 14, M3). One bounded read; a no-op when nothing was merged."""
        if not payload.entities:
            return payload
        redirects = await self._repo.resolve_merged_names(
            user_id=user_id,
            normalized_names=sorted({normalize_name(e.name) for e in payload.entities}),
        )
        if not redirects:
            return payload

        def canonical(name: str) -> str:
            hit = redirects.get(normalize_name(name))
            return hit[0] if hit else name

        entities = []
        for e in payload.entities:
            hit = redirects.get(normalize_name(e.name))
            if hit is None:
                entities.append(e)
                continue
            aliases = list(dict.fromkeys([*e.aliases, e.name]))
            entities.append(e.model_copy(update={"name": hit[0], "aliases": aliases}))
        relationships = [
            r.model_copy(update={"source": canonical(r.source), "target": canonical(r.target)})
            for r in payload.relationships
        ]
        return payload.model_copy(update={"entities": entities, "relationships": relationships})

    def _build_entities(
        self, payload: MemoryCreate
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Normalize entity inputs and reject duplicates within one request.

        The returned ids are only used if the entity does not already exist; the
        MERGE in the repository keeps the existing node otherwise.
        """
        entities: list[dict[str, Any]] = []
        by_normalized: dict[str, str] = {}
        for item in payload.entities:
            normalized = normalize_name(item.name)
            if normalized in by_normalized:
                raise ValidationError(
                    "The same entity is listed twice in this memory",
                    details={"name": item.name, "normalized_name": normalized},
                )
            by_normalized[normalized] = item.name
            entities.append(
                {
                    "id": self._new_id(),
                    "name": item.name.strip(),
                    "normalized_name": normalized,
                    "entity_type": item.entity_type.value,
                    "aliases": [normalize_name(a) for a in item.aliases],
                }
            )
        return entities, by_normalized

    def _build_relationships(
        self,
        payload: MemoryCreate,
        by_normalized: dict[str, str],
        *,
        user_id: str,
        memory_id: str,
        now: datetime,
        open_start: bool = False,
    ) -> list[dict[str, Any]]:
        """Validate asserted edges and attach provenance plus a validity window."""
        relationships: list[dict[str, Any]] = []
        for rel in payload.relationships:
            source_norm = normalize_name(rel.source)
            target_norm = normalize_name(rel.target)
            for norm, raw in ((source_norm, rel.source), (target_norm, rel.target)):
                if norm not in by_normalized:
                    raise ValidationError(
                        "Relationship endpoint is not listed in this memory's entities",
                        details={
                            "endpoint": raw,
                            "known_entities": sorted(by_normalized.values()),
                        },
                    )
            if source_norm == target_norm:
                raise ValidationError(
                    "A relationship cannot connect an entity to itself",
                    details={"entity": rel.source, "predicate": rel.predicate},
                )
            validate_window(rel.valid_from, rel.valid_until)

            props: dict[str, Any] = {
                "id": self._new_id(),
                "user_id": user_id,
                "predicate": rel.predicate,
                "status": payload.status.value,
                "confidence": rel.confidence,
                # Provenance: which memory asserted this edge.
                "memory_id": memory_id,
                "valid_from": rel.valid_from or payload.valid_from or (None if open_start else now),
                "valid_until": rel.valid_until or payload.valid_until,
                "created_at": now,
                "updated_at": now,
            }
            relationships.append(
                {
                    "source_normalized": source_norm,
                    "target_normalized": target_norm,
                    "props": {k: v for k, v in props.items() if v is not None},
                }
            )
        return relationships

    def _build_source(self, payload: MemoryCreate) -> dict[str, Any] | None:
        if payload.source is None:
            return None
        src = payload.source
        return {
            "id": self._new_id(),
            "source_type": src.source_type.value,
            "reference": src.reference,
            "location": src.location,
            # Memories from the same conversation or document share one Source node,
            # which is what lets an entity page report "3 conversations, 2 documents".
            "dedup_key": f"{src.reference or ''}||{src.location or ''}",
        }
