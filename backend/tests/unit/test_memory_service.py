"""Memory lifecycle service tests.

This is where the rules that define the product live: a changed fact must not erase
the old one, a retraction must be recorded rather than deleted, and one user must
never reach another user's memory.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.enums import MemoryStatus, MemoryType
from app.repositories.memory_repository import MemoryRepository
from app.schemas.memory import MemoryCreate, MemoryUpdate
from app.services.memory_service import MemoryService
from tests.conftest import JAN, JUN, MAR, MAY, FrozenClock, SequentialIds
from tests.unit.fakes import FakeMemoryRepository

USER = "user-a"
OTHER_USER = "user-b"


@pytest.fixture
def store() -> FakeMemoryRepository:
    return FakeMemoryRepository()


@pytest.fixture
def service(store: FakeMemoryRepository, clock: FrozenClock, ids: SequentialIds) -> MemoryService:
    return MemoryService(store, now_fn=clock, id_fn=ids)  # type: ignore[arg-type]


def uses_payload(target: str = "EasyOCR", **overrides: object) -> MemoryCreate:
    """"Drawing Extractor uses <target>" with both entities declared."""
    data: dict[str, object] = {
        "content": f"Drawing Extractor uses {target}",
        "memory_type": "semantic",
        "entities": [
            {"name": "Drawing Extractor", "entity_type": "project"},
            {"name": target, "entity_type": "technology"},
        ],
        "relationships": [
            {"source": "Drawing Extractor", "predicate": "uses", "target": target}
        ],
        "source": {"source_type": "conversation", "reference": "conv-1"},
    }
    data.update(overrides)
    return MemoryCreate.model_validate(data)


class TestCreate:
    async def test_stores_content_entities_relationships_and_source(
        self, service: MemoryService
    ) -> None:
        detail = await service.create(user_id=USER, payload=uses_payload())
        assert detail.memory.content == "Drawing Extractor uses EasyOCR"
        assert detail.memory.status is MemoryStatus.ACTIVE
        assert detail.memory.user_id == USER
        assert {e.name for e in detail.entities} == {"Drawing Extractor", "EasyOCR"}
        assert [r.predicate for r in detail.relationships] == ["uses"]
        assert len(detail.sources) == 1
        assert detail.sources[0].reference == "conv-1"

    async def test_valid_from_defaults_to_the_recording_time(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        """Every stored fact needs a start, so `as_of` queries are total."""
        detail = await service.create(user_id=USER, payload=uses_payload())
        assert detail.memory.valid_from == clock.now
        assert detail.memory.valid_until is None

    async def test_explicit_validity_window_is_preserved(
        self, service: MemoryService
    ) -> None:
        detail = await service.create(
            user_id=USER,
            payload=uses_payload(valid_from=JAN.isoformat(), valid_until=MAY.isoformat()),
        )
        assert detail.memory.valid_from == JAN
        assert detail.memory.valid_until == MAY

    async def test_relationship_carries_provenance_back_to_the_memory(
        self, service: MemoryService
    ) -> None:
        detail = await service.create(user_id=USER, payload=uses_payload())
        assert detail.relationships[0].memory_id == detail.memory.id

    async def test_entity_is_reused_across_memories(
        self, service: MemoryService, store: FakeMemoryRepository
    ) -> None:
        """A second mention must attach to the existing node, not create a twin."""
        first = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        second = await service.create(user_id=USER, payload=uses_payload("Gemini"))
        project_ids = {
            e.id
            for detail in (first, second)
            for e in detail.entities
            if e.name == "Drawing Extractor"
        }
        assert len(project_ids) == 1
        assert len([k for k in store.entities if k[0] == USER]) == 3

    async def test_case_variant_entity_names_resolve_to_one_node(
        self, service: MemoryService, store: FakeMemoryRepository
    ) -> None:
        await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        await service.create(user_id=USER, payload=uses_payload("easyocr"))
        assert ("user-a", "easyocr") in store.entities
        assert len([k for k in store.entities if k[0] == USER]) == 2

    async def test_reads_back_what_was_stored(self, service: MemoryService) -> None:
        """The response must come from the store, not be assembled optimistically."""
        created = await service.create(user_id=USER, payload=uses_payload())
        fetched = await service.get(user_id=USER, memory_id=created.memory.id)
        assert fetched == created


class TestCreateValidation:
    async def test_rejects_relationship_endpoint_not_declared_as_entity(
        self, service: MemoryService
    ) -> None:
        """A typo must not silently spawn a new entity."""
        payload = MemoryCreate.model_validate(
            {
                "content": "x",
                "entities": [{"name": "Drawing Extractor", "entity_type": "project"}],
                "relationships": [
                    {"source": "Drawing Extractor", "predicate": "uses", "target": "Ghost"}
                ],
            }
        )
        with pytest.raises(ValidationError, match="not listed"):
            await service.create(user_id=USER, payload=payload)

    async def test_rejects_self_referencing_relationship(
        self, service: MemoryService
    ) -> None:
        payload = MemoryCreate.model_validate(
            {
                "content": "x",
                "entities": [{"name": "Thing", "entity_type": "concept"}],
                "relationships": [
                    {"source": "Thing", "predicate": "uses", "target": "thing"}
                ],
            }
        )
        with pytest.raises(ValidationError, match="itself"):
            await service.create(user_id=USER, payload=payload)

    async def test_rejects_duplicate_entities_in_one_memory(
        self, service: MemoryService
    ) -> None:
        payload = MemoryCreate.model_validate(
            {
                "content": "x",
                "entities": [
                    {"name": "EasyOCR", "entity_type": "technology"},
                    {"name": "easyocr", "entity_type": "technology"},
                ],
            }
        )
        with pytest.raises(ValidationError, match="twice"):
            await service.create(user_id=USER, payload=payload)

    async def test_rejects_reversed_validity_window(self, service: MemoryService) -> None:
        payload = uses_payload(valid_from=MAY.isoformat(), valid_until=JAN.isoformat())
        with pytest.raises(ValidationError, match="strictly after"):
            await service.create(user_id=USER, payload=payload)

    async def test_rejects_unknown_supersession_target(
        self, service: MemoryService
    ) -> None:
        payload = uses_payload(supersedes=["does-not-exist"])
        with pytest.raises(NotFoundError):
            await service.create(user_id=USER, payload=payload)

    async def test_failed_supersession_leaves_no_partial_record(
        self, service: MemoryService, store: FakeMemoryRepository
    ) -> None:
        """A rejected write must not leave a half-created memory behind."""
        with pytest.raises(NotFoundError):
            await service.create(user_id=USER, payload=uses_payload(supersedes=["ghost"]))
        assert store.memories == {}
        assert store.relationships == []


class TestSupersession:
    """The core temporal requirement: a changed fact must not erase the old one."""

    async def test_superseded_memory_is_preserved_not_deleted(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        new = await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )

        still_there = await service.get(user_id=USER, memory_id=old.memory.id)
        assert still_there.memory.status is MemoryStatus.SUPERSEDED
        assert still_there.memory.content == "Drawing Extractor uses EasyOCR"
        assert still_there.memory.valid_until == MAY
        assert still_there.superseded_by == [new.memory.id]
        assert new.supersedes == [old.memory.id]

    async def test_superseded_relationship_window_is_closed(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )
        reloaded = await service.get(user_id=USER, memory_id=old.memory.id)
        edge = reloaded.relationships[0]
        assert edge.status is MemoryStatus.SUPERSEDED
        assert edge.valid_from == JAN
        assert edge.valid_until == MAY

    async def test_replacement_starts_where_the_old_fact_ended(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        """Adjacent half-open windows: no overlap, no gap."""
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        new = await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )
        closed = await service.get(user_id=USER, memory_id=old.memory.id)
        assert closed.memory.valid_until == new.memory.valid_from

    async def test_explicit_end_date_is_not_overwritten(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        old = await service.create(
            user_id=USER, payload=uses_payload("EasyOCR", valid_until=MAR.isoformat())
        )
        clock.advance_to(MAY)
        await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )
        reloaded = await service.get(user_id=USER, memory_id=old.memory.id)
        assert reloaded.memory.valid_until == MAR, "a stated end date must be respected"

    async def test_cannot_supersede_another_users_memory(
        self, service: MemoryService
    ) -> None:
        theirs = await service.create(user_id=OTHER_USER, payload=uses_payload("EasyOCR"))
        with pytest.raises(NotFoundError):
            await service.create(
                user_id=USER,
                payload=uses_payload("PaddleOCR", supersedes=[theirs.memory.id]),
            )
        untouched = await service.get(user_id=OTHER_USER, memory_id=theirs.memory.id)
        assert untouched.memory.status is MemoryStatus.ACTIVE


class TestUpdate:
    async def test_updates_metadata(self, service: MemoryService) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        updated = await service.update(
            user_id=USER,
            memory_id=created.memory.id,
            payload=MemoryUpdate(confidence=0.5, memory_type=MemoryType.EPISODIC),
        )
        assert updated.confidence == 0.5
        assert updated.memory_type is MemoryType.EPISODIC

    async def test_content_cannot_be_updated(self) -> None:
        """Rewriting stored text in place would destroy the historical record."""
        with pytest.raises(Exception, match="content"):
            MemoryUpdate.model_validate({"content": "rewritten"})

    async def test_rejects_empty_update(self, service: MemoryService) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        with pytest.raises(ValidationError, match="No updatable fields"):
            await service.update(
                user_id=USER, memory_id=created.memory.id, payload=MemoryUpdate()
            )

    async def test_rejects_illegal_status_transition(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )
        with pytest.raises(ConflictError, match="Cannot change status"):
            await service.update(
                user_id=USER,
                memory_id=old.memory.id,
                payload=MemoryUpdate(status=MemoryStatus.ACTIVE),
            )

    async def test_rejects_update_producing_a_reversed_window(
        self, service: MemoryService
    ) -> None:
        """The window is validated as it will be *after* the update."""
        created = await service.create(
            user_id=USER, payload=uses_payload(valid_from=MAY.isoformat())
        )
        with pytest.raises(ValidationError, match="strictly after"):
            await service.update(
                user_id=USER,
                memory_id=created.memory.id,
                payload=MemoryUpdate(valid_until=JAN),
            )

    async def test_cannot_update_another_users_memory(
        self, service: MemoryService
    ) -> None:
        theirs = await service.create(user_id=OTHER_USER, payload=uses_payload())
        with pytest.raises(NotFoundError):
            await service.update(
                user_id=USER,
                memory_id=theirs.memory.id,
                payload=MemoryUpdate(confidence=0.1),
            )

    async def test_update_of_missing_memory_is_not_found(
        self, service: MemoryService
    ) -> None:
        with pytest.raises(NotFoundError):
            await service.update(
                user_id=USER, memory_id="ghost", payload=MemoryUpdate(confidence=0.1)
            )


class TestInvalidate:
    async def test_marks_invalid_and_records_the_reason(
        self, service: MemoryService
    ) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        result = await service.invalidate(
            user_id=USER, memory_id=created.memory.id, reason="I misremembered"
        )
        assert result.status is MemoryStatus.INVALID
        assert result.invalidation_reason == "I misremembered"

    async def test_memory_survives_invalidation(self, service: MemoryService) -> None:
        """Retraction is recorded, not erased: the record must still be readable."""
        created = await service.create(user_id=USER, payload=uses_payload())
        await service.invalidate(user_id=USER, memory_id=created.memory.id, reason=None)
        still_there = await service.get(user_id=USER, memory_id=created.memory.id)
        assert still_there.memory.content == "Drawing Extractor uses EasyOCR"

    async def test_closes_the_asserted_relationships(self, service: MemoryService) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        await service.invalidate(user_id=USER, memory_id=created.memory.id, reason=None)
        reloaded = await service.get(user_id=USER, memory_id=created.memory.id)
        assert all(r.status is MemoryStatus.INVALID for r in reloaded.relationships)

    async def test_is_idempotent(self, service: MemoryService) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        first = await service.invalidate(
            user_id=USER, memory_id=created.memory.id, reason="a"
        )
        second = await service.invalidate(
            user_id=USER, memory_id=created.memory.id, reason="a"
        )
        assert first.status is second.status is MemoryStatus.INVALID

    async def test_archived_memory_cannot_be_invalidated(
        self, service: MemoryService
    ) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        await service.update(
            user_id=USER,
            memory_id=created.memory.id,
            payload=MemoryUpdate(status=MemoryStatus.ARCHIVED),
        )
        with pytest.raises(ConflictError):
            await service.invalidate(
                user_id=USER, memory_id=created.memory.id, reason=None
            )

    async def test_cannot_invalidate_another_users_memory(
        self, service: MemoryService
    ) -> None:
        theirs = await service.create(user_id=OTHER_USER, payload=uses_payload())
        with pytest.raises(NotFoundError):
            await service.invalidate(
                user_id=USER, memory_id=theirs.memory.id, reason=None
            )
        untouched = await service.get(user_id=OTHER_USER, memory_id=theirs.memory.id)
        assert untouched.memory.status is MemoryStatus.ACTIVE


class TestRetrievalAndIsolation:
    async def test_missing_memory_is_not_found(self, service: MemoryService) -> None:
        with pytest.raises(NotFoundError):
            await service.get(user_id=USER, memory_id="ghost")

    async def test_another_users_memory_reads_as_not_found(
        self, service: MemoryService
    ) -> None:
        """Reported as not-found, never forbidden: existence must not be confirmed."""
        theirs = await service.create(user_id=OTHER_USER, payload=uses_payload())
        with pytest.raises(NotFoundError) as exc:
            await service.get(user_id=USER, memory_id=theirs.memory.id)
        assert exc.value.status_code == 404

    async def test_search_never_returns_another_users_memories(
        self, service: MemoryService
    ) -> None:
        await service.create(user_id=OTHER_USER, payload=uses_payload("EasyOCR"))
        await service.create(user_id=USER, payload=uses_payload("Gemini"))
        results = await service.search(user_id=USER)
        assert results.total == 1
        assert all(m.user_id == USER for m in results.items)
        assert "Gemini" in results.items[0].content


class TestSearch:
    async def test_matches_content_case_insensitively(
        self, service: MemoryService
    ) -> None:
        await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        assert (await service.search(user_id=USER, query="easyocr")).total == 1
        assert (await service.search(user_id=USER, query="EASYOCR")).total == 1

    async def test_hides_invalid_memories_by_default(
        self, service: MemoryService
    ) -> None:
        created = await service.create(user_id=USER, payload=uses_payload())
        await service.invalidate(user_id=USER, memory_id=created.memory.id, reason=None)
        assert (await service.search(user_id=USER)).total == 0
        assert (await service.search(user_id=USER, include_excluded=True)).total == 1

    async def test_keeps_superseded_memories_visible_by_default(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        """Inspecting superseded memory is a product requirement, not an edge case."""
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )
        assert (await service.search(user_id=USER)).total == 2

    async def test_filters_by_status(self, service: MemoryService, clock: FrozenClock) -> None:
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )
        active = await service.search(user_id=USER, statuses=[MemoryStatus.ACTIVE])
        assert [m.content for m in active.items] == ["Drawing Extractor uses PaddleOCR"]

    async def test_filters_by_entity(self, service: MemoryService) -> None:
        await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        await service.create(user_id=USER, payload=uses_payload("Gemini"))
        result = await service.search(user_id=USER, entity_name="gemini")
        assert result.total == 1

    async def test_as_of_returns_the_fact_true_at_that_time(
        self, service: MemoryService, clock: FrozenClock
    ) -> None:
        """The headline temporal question: what was true in March?"""
        old = await service.create(user_id=USER, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await service.create(
            user_id=USER, payload=uses_payload("PaddleOCR", supersedes=[old.memory.id])
        )

        march = await service.search(user_id=USER, as_of=MAR)
        assert [m.content for m in march.items] == ["Drawing Extractor uses EasyOCR"]

        june = await service.search(user_id=USER, as_of=JUN)
        assert [m.content for m in june.items] == ["Drawing Extractor uses PaddleOCR"]

    async def test_pagination(self, service: MemoryService) -> None:
        for name in ("EasyOCR", "Gemini", "OpenCV"):
            await service.create(user_id=USER, payload=uses_payload(name))
        page = await service.search(user_id=USER, limit=2, offset=0)
        assert len(page.items) == 2
        assert page.total == 3
        assert len((await service.search(user_id=USER, limit=2, offset=2)).items) == 1


def test_service_depends_only_on_the_repository_interface() -> None:
    """The fake must satisfy the same call signature as the real repository.

    If the real repository's method signatures drift from the double, these unit
    tests would keep passing while production broke -- so the shapes are compared.
    """
    import inspect

    for name in ("create", "get", "update", "invalidate", "search", "count"):
        real = inspect.signature(getattr(MemoryRepository, name))
        fake = inspect.signature(getattr(FakeMemoryRepository, name))
        assert set(real.parameters) == set(fake.parameters), name
