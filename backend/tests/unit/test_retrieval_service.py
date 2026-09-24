"""Deterministic graph retrieval tests.

The scenario is the one from PRODUCT.md and the seed script:

    Drawing Extractor --uses--> EasyOCR    [January, May)   superseded
    Drawing Extractor --uses--> PaddleOCR  [May, ...)       active
    Drawing Extractor --uses--> Gemini     [January, ...)   active
    EasyOCR           --related_to--> OCR  [January, ...)   active

so that "what am I using now?" and "what was I using in March?" are both checkable.
"""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.models.enums import Direction, MemoryStatus
from app.repositories.entity_repository import EntityRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService
from tests.conftest import JAN, JUN, MAR, MAY, FrozenClock, SequentialIds
from tests.unit.fakes import FakeEntityRepository, FakeMemoryRepository

USER = "user-a"
OTHER_USER = "user-b"
MAX_HOPS = 4


@pytest.fixture
def store() -> FakeMemoryRepository:
    return FakeMemoryRepository()


@pytest.fixture
def memory_service(
    store: FakeMemoryRepository, clock: FrozenClock, ids: SequentialIds
) -> MemoryService:
    return MemoryService(store, now_fn=clock, id_fn=ids)  # type: ignore[arg-type]


@pytest.fixture
def retrieval(store: FakeMemoryRepository, clock: FrozenClock) -> RetrievalService:
    return RetrievalService(
        FakeEntityRepository(store),  # type: ignore[arg-type]
        max_hops=MAX_HOPS,
        now_fn=clock,
    )


def _memory(content: str, pairs: list[tuple[str, str, str, str]], **kw: object) -> MemoryCreate:
    """Build a memory from (source, predicate, target, target_type) tuples."""
    entities: dict[str, str] = {}
    rels = []
    for src, predicate, tgt, tgt_type in pairs:
        entities.setdefault(src, "project")
        entities.setdefault(tgt, tgt_type)
        rels.append({"source": src, "predicate": predicate, "target": tgt})
    data: dict[str, object] = {
        "content": content,
        "entities": [{"name": n, "entity_type": t} for n, t in entities.items()],
        "relationships": rels,
    }
    data.update(kw)
    return MemoryCreate.model_validate(data)


@pytest.fixture
async def scenario(
    memory_service: MemoryService, store: FakeMemoryRepository, clock: FrozenClock
) -> dict[str, str]:
    """Build the graph above and return a name -> entity id map."""
    clock.advance_to(JAN)
    easy = await memory_service.create(
        user_id=USER,
        payload=_memory(
            "Drawing Extractor uses EasyOCR",
            [("Drawing Extractor", "uses", "EasyOCR", "technology")],
        ),
    )
    await memory_service.create(
        user_id=USER,
        payload=_memory(
            "Drawing Extractor uses Gemini",
            [("Drawing Extractor", "uses", "Gemini", "technology")],
        ),
    )
    await memory_service.create(
        user_id=USER,
        payload=_memory(
            "EasyOCR is an OCR library",
            [("EasyOCR", "related_to", "OCR", "concept")],
        ),
    )
    clock.advance_to(MAY)
    await memory_service.create(
        user_id=USER,
        payload=_memory(
            "Drawing Extractor uses PaddleOCR",
            [("Drawing Extractor", "uses", "PaddleOCR", "technology")],
            supersedes=[easy.memory.id],
        ),
    )
    clock.advance_to(JUN)
    return {
        entity.name: entity.id
        for (uid, _), entity in store.entities.items()
        if uid == USER
    }


class TestEntityRetrieval:
    async def test_returns_entity_with_counts(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        detail = await retrieval.get_entity(
            user_id=USER, entity_id=scenario["Drawing Extractor"]
        )
        assert detail.entity.name == "Drawing Extractor"
        assert detail.entity.entity_type.value == "project"
        assert detail.memory_count == 3
        assert detail.relationship_count == 3

    async def test_missing_entity_is_not_found(self, retrieval: RetrievalService) -> None:
        with pytest.raises(NotFoundError):
            await retrieval.get_entity(user_id=USER, entity_id="ghost")

    async def test_resolves_by_name_case_insensitively(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        found = await retrieval.find_entity_by_name(user_id=USER, name="  dRAWING  extractor ")
        assert found.id == scenario["Drawing Extractor"]

    async def test_unknown_name_is_not_found(self, retrieval: RetrievalService) -> None:
        with pytest.raises(NotFoundError):
            await retrieval.find_entity_by_name(user_id=USER, name="Nonexistent")

    async def test_search_matches_substring(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        results = await retrieval.search_entities(
            user_id=USER, query="ocr", limit=10, offset=0
        )
        assert {e.name for e in results} == {"EasyOCR", "PaddleOCR", "OCR"}


class TestRelationships:
    async def test_returns_all_incident_edges(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        views = await retrieval.get_relationships(
            user_id=USER, entity_id=scenario["Drawing Extractor"]
        )
        assert {v.target_entity.name for v in views} == {"EasyOCR", "Gemini", "PaddleOCR"}

    async def test_direction_filter(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        outgoing = await retrieval.get_relationships(
            user_id=USER, entity_id=scenario["EasyOCR"], direction=Direction.OUTGOING
        )
        incoming = await retrieval.get_relationships(
            user_id=USER, entity_id=scenario["EasyOCR"], direction=Direction.INCOMING
        )
        assert {v.target_entity.name for v in outgoing} == {"OCR"}
        assert {v.source_entity.name for v in incoming} == {"Drawing Extractor"}

    async def test_as_of_returns_only_edges_valid_then(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        march = await retrieval.get_relationships(
            user_id=USER, entity_id=scenario["Drawing Extractor"], as_of=MAR
        )
        assert {v.target_entity.name for v in march} == {"EasyOCR", "Gemini"}

        june = await retrieval.get_relationships(
            user_id=USER, entity_id=scenario["Drawing Extractor"], as_of=JUN
        )
        assert {v.target_entity.name for v in june} == {"PaddleOCR", "Gemini"}

    async def test_superseded_edge_is_still_returned_with_its_status(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        """History is preserved: the old edge is still there, marked superseded."""
        views = await retrieval.get_relationships(
            user_id=USER, entity_id=scenario["Drawing Extractor"]
        )
        easy = next(v for v in views if v.target_entity.name == "EasyOCR")
        assert easy.relationship.status is MemoryStatus.SUPERSEDED
        assert easy.relationship.valid_from == JAN
        assert easy.relationship.valid_until == MAY


class TestEntityState:
    """The currently_uses / previously_used projection."""

    async def test_current_state_now(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        state = await retrieval.get_entity_state(
            user_id=USER, entity_id=scenario["Drawing Extractor"]
        )
        assert {v.target_entity.name for v in state.current} == {"PaddleOCR", "Gemini"}
        assert {v.target_entity.name for v in state.historical} == {"EasyOCR"}

    async def test_historical_state_in_march(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        """Asking about March must return what was true in March."""
        state = await retrieval.get_entity_state(
            user_id=USER, entity_id=scenario["Drawing Extractor"], as_of=MAR
        )
        assert {v.target_entity.name for v in state.current} == {"EasyOCR", "Gemini"}
        assert state.historical == []
        assert state.as_of == MAR

    async def test_a_fact_not_yet_true_appears_in_neither_bucket(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        """In March, PaddleOCR was neither current nor history -- it had not happened."""
        state = await retrieval.get_entity_state(
            user_id=USER, entity_id=scenario["Drawing Extractor"], as_of=MAR
        )
        names = {v.target_entity.name for v in state.current + state.historical}
        assert "PaddleOCR" not in names

    async def test_state_defaults_to_now(
        self, retrieval: RetrievalService, scenario: dict[str, str], clock: FrozenClock
    ) -> None:
        state = await retrieval.get_entity_state(
            user_id=USER, entity_id=scenario["Drawing Extractor"]
        )
        assert state.as_of == clock.now

    async def test_invalidated_relationships_are_excluded(
        self,
        retrieval: RetrievalService,
        memory_service: MemoryService,
        store: FakeMemoryRepository,
        scenario: dict[str, str],
    ) -> None:
        gemini_memory = next(
            mid
            for (uid, mid), props in store.memories.items()
            if uid == USER and "Gemini" in props["content"]
        )
        await memory_service.invalidate(
            user_id=USER, memory_id=gemini_memory, reason="wrong"
        )
        state = await retrieval.get_entity_state(
            user_id=USER, entity_id=scenario["Drawing Extractor"]
        )
        names = {v.target_entity.name for v in state.current + state.historical}
        assert "Gemini" not in names


class TestMultiHop:
    async def test_one_hop_returns_direct_neighbours(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        graph = await retrieval.get_neighbourhood(
            user_id=USER, entity_id=scenario["Drawing Extractor"], hops=1
        )
        assert {n.entity.name for n in graph.nodes} == {"EasyOCR", "Gemini", "PaddleOCR"}
        assert all(n.hops == 1 for n in graph.nodes)

    async def test_two_hops_reaches_the_indirect_concept(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        """Drawing Extractor -> EasyOCR -> OCR: the structural question vector
        search would miss (CLAUDE.md section 19)."""
        graph = await retrieval.get_neighbourhood(
            user_id=USER, entity_id=scenario["Drawing Extractor"], hops=2
        )
        by_name = {n.entity.name: n.hops for n in graph.nodes}
        assert by_name["OCR"] == 2
        assert by_name["EasyOCR"] == 1

    async def test_root_is_excluded_from_the_node_list(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        graph = await retrieval.get_neighbourhood(
            user_id=USER, entity_id=scenario["Drawing Extractor"], hops=2
        )
        assert graph.root.name == "Drawing Extractor"
        assert all(n.entity.id != graph.root.id for n in graph.nodes)

    async def test_shortest_distance_is_reported(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        graph = await retrieval.get_neighbourhood(
            user_id=USER, entity_id=scenario["OCR"], hops=3
        )
        by_name = {n.entity.name: n.hops for n in graph.nodes}
        assert by_name["EasyOCR"] == 1
        assert by_name["Drawing Extractor"] == 2
        assert by_name["Gemini"] == 3

    async def test_hop_limit_is_enforced(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(ValidationError, match="between 1 and"):
            await retrieval.get_neighbourhood(
                user_id=USER, entity_id=scenario["Drawing Extractor"], hops=MAX_HOPS + 1
            )

    async def test_zero_hops_is_rejected(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(ValidationError):
            await retrieval.get_neighbourhood(
                user_id=USER, entity_id=scenario["Drawing Extractor"], hops=0
            )


class TestIsolation:
    async def test_cannot_read_another_users_entity(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(NotFoundError):
            await retrieval.get_entity(
                user_id=OTHER_USER, entity_id=scenario["Drawing Extractor"]
            )

    async def test_cannot_traverse_into_another_users_graph(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(NotFoundError):
            await retrieval.get_neighbourhood(
                user_id=OTHER_USER, entity_id=scenario["Drawing Extractor"], hops=2
            )

    async def test_entity_search_is_scoped_to_the_user(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        assert (
            await retrieval.search_entities(
                user_id=OTHER_USER, query="ocr", limit=10, offset=0
            )
            == []
        )

    async def test_relationships_of_another_users_entity_are_not_found(
        self, retrieval: RetrievalService, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(NotFoundError):
            await retrieval.get_relationships(
                user_id=OTHER_USER, entity_id=scenario["Drawing Extractor"]
            )


def test_fake_entity_repository_matches_the_real_interface() -> None:
    import inspect

    for name in ("get", "find_by_normalized_name", "search", "relationships",
                 "memories", "neighbourhood"):
        real = inspect.signature(getattr(EntityRepository, name))
        fake = inspect.signature(getattr(FakeEntityRepository, name))
        assert set(real.parameters) == set(fake.parameters), name
