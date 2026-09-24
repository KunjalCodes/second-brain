"""Graph retrieval against real Neo4j: traversal, temporal state, isolation.

The variable-length traversal and the path-scoped user filter can only be verified
here -- no test double reproduces Cypher's path semantics.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.graph.connection import GraphClient
from app.models.enums import Direction, MemoryStatus
from app.repositories.entity_repository import EntityRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService
from tests.conftest import JAN, JUN, MAR, MAY, FrozenClock

pytestmark = pytest.mark.integration


def rel_memory(
    content: str,
    source: str,
    source_type: str,
    predicate: str,
    target: str,
    target_type: str,
    **kw: object,
) -> MemoryCreate:
    data: dict[str, object] = {
        "content": content,
        "entities": [
            {"name": source, "entity_type": source_type},
            {"name": target, "entity_type": target_type},
        ],
        "relationships": [{"source": source, "predicate": predicate, "target": target}],
    }
    data.update(kw)
    return MemoryCreate.model_validate(data)


@pytest.fixture
async def scenario(
    memories: MemoryService,
    retrieval: RetrievalService,
    user_id: str,
    clock: FrozenClock,
    cleanup: None,
) -> dict[str, str]:
    """The documented narrative, written to the real graph."""
    clock.advance_to(JAN)
    easy = await memories.create(
        user_id=user_id,
        payload=rel_memory(
            "Drawing Extractor uses EasyOCR",
            "Drawing Extractor",
            "project",
            "uses",
            "EasyOCR",
            "technology",
        ),
    )
    await memories.create(
        user_id=user_id,
        payload=rel_memory(
            "Drawing Extractor uses Gemini",
            "Drawing Extractor",
            "project",
            "uses",
            "Gemini",
            "technology",
        ),
    )
    await memories.create(
        user_id=user_id,
        payload=rel_memory(
            "EasyOCR is an OCR library",
            "EasyOCR",
            "technology",
            "related_to",
            "OCR",
            "concept",
        ),
    )
    clock.advance_to(MAY)
    await memories.create(
        user_id=user_id,
        payload=rel_memory(
            "Drawing Extractor uses PaddleOCR",
            "Drawing Extractor",
            "project",
            "uses",
            "PaddleOCR",
            "technology",
            supersedes=[easy.memory.id],
        ),
    )
    clock.advance_to(JUN)
    ids = {}
    for name in ("Drawing Extractor", "EasyOCR", "Gemini", "PaddleOCR", "OCR"):
        entity = await retrieval.find_entity_by_name(user_id=user_id, name=name)
        ids[name] = entity.id
    return ids


class TestEntityRetrieval:
    async def test_entity_detail_counts(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        detail = await retrieval.get_entity(
            user_id=user_id, entity_id=scenario["Drawing Extractor"]
        )
        assert detail.entity.name == "Drawing Extractor"
        assert detail.memory_count == 3
        assert detail.relationship_count == 3

    async def test_resolves_name_case_insensitively(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        found = await retrieval.find_entity_by_name(user_id=user_id, name="  drawing   EXTRACTOR ")
        assert found.id == scenario["Drawing Extractor"]

    async def test_entity_search_matches_substring(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        results = await retrieval.search_entities(user_id=user_id, query="ocr", limit=10, offset=0)
        assert {e.name for e in results} == {"EasyOCR", "PaddleOCR", "OCR"}

    async def test_entity_memories(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        found = await retrieval.get_entity_memories(
            user_id=user_id, entity_id=scenario["EasyOCR"], limit=10, offset=0
        )
        assert len(found) == 2


class TestRelationships:
    async def test_all_incident_edges(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        views = await retrieval.get_relationships(
            user_id=user_id, entity_id=scenario["Drawing Extractor"]
        )
        assert {v.target_entity.name for v in views} == {
            "EasyOCR",
            "Gemini",
            "PaddleOCR",
        }

    async def test_direction_filtering(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        outgoing = await retrieval.get_relationships(
            user_id=user_id,
            entity_id=scenario["EasyOCR"],
            direction=Direction.OUTGOING,
        )
        incoming = await retrieval.get_relationships(
            user_id=user_id,
            entity_id=scenario["EasyOCR"],
            direction=Direction.INCOMING,
        )
        assert {v.target_entity.name for v in outgoing} == {"OCR"}
        assert {v.source_entity.name for v in incoming} == {"Drawing Extractor"}

    async def test_superseded_edge_retains_its_window(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        views = await retrieval.get_relationships(
            user_id=user_id, entity_id=scenario["Drawing Extractor"]
        )
        easy = next(v for v in views if v.target_entity.name == "EasyOCR")
        assert easy.relationship.status is MemoryStatus.SUPERSEDED
        assert easy.relationship.valid_from == JAN
        assert easy.relationship.valid_until == MAY


class TestTemporalState:
    async def test_current_state(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        state = await retrieval.get_entity_state(
            user_id=user_id, entity_id=scenario["Drawing Extractor"]
        )
        assert {v.target_entity.name for v in state.current} == {"PaddleOCR", "Gemini"}
        assert {v.target_entity.name for v in state.historical} == {"EasyOCR"}

    async def test_state_as_of_march(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        """ "What was I using in March?" must answer EasyOCR."""
        state = await retrieval.get_entity_state(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], as_of=MAR
        )
        assert {v.target_entity.name for v in state.current} == {"EasyOCR", "Gemini"}
        assert state.historical == []

    async def test_relationships_as_of_march(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        views = await retrieval.get_relationships(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], as_of=MAR
        )
        assert {v.target_entity.name for v in views} == {"EasyOCR", "Gemini"}


class TestMultiHop:
    async def test_one_hop(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        graph_result = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], hops=1
        )
        assert {n.entity.name for n in graph_result.nodes} == {
            "EasyOCR",
            "Gemini",
            "PaddleOCR",
        }

    async def test_two_hops_reaches_the_concept(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        graph_result = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], hops=2
        )
        by_name = {n.entity.name: n.hops for n in graph_result.nodes}
        assert by_name["OCR"] == 2
        assert by_name["EasyOCR"] == 1

    async def test_shortest_path_distance_is_reported(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        graph_result = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=scenario["OCR"], hops=3
        )
        by_name = {n.entity.name: n.hops for n in graph_result.nodes}
        assert by_name == {
            "EasyOCR": 1,
            "Drawing Extractor": 2,
            "Gemini": 3,
            "PaddleOCR": 3,
        }

    async def test_root_excluded_and_edges_returned(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        graph_result = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], hops=2
        )
        assert all(n.entity.id != graph_result.root.id for n in graph_result.nodes)
        assert len(graph_result.relationships) == 4

    async def test_hop_bound_is_enforced_before_querying(
        self, retrieval: RetrievalService, user_id: str, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(ValidationError):
            await retrieval.get_neighbourhood(
                user_id=user_id, entity_id=scenario["Drawing Extractor"], hops=50
            )


class TestIsolation:
    """Cross-user access must fail against the real database, not just the double."""

    async def test_cannot_read_another_users_memory(
        self, memories: MemoryService, user_id: str, other_user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(
            user_id=user_id,
            payload=rel_memory("Private note", "A", "project", "uses", "B", "technology"),
        )
        with pytest.raises(NotFoundError):
            await memories.get(user_id=other_user_id, memory_id=created.memory.id)

    async def test_cannot_read_another_users_entity(
        self, retrieval: RetrievalService, other_user_id: str, scenario: dict[str, str]
    ) -> None:
        with pytest.raises(NotFoundError):
            await retrieval.get_entity(
                user_id=other_user_id, entity_id=scenario["Drawing Extractor"]
            )

    async def test_search_is_scoped(
        self, memories: MemoryService, other_user_id: str, scenario: dict[str, str]
    ) -> None:
        assert (await memories.search(user_id=other_user_id)).total == 0

    async def test_traversal_cannot_cross_into_another_users_subgraph(
        self,
        graph: GraphClient,
        memories: MemoryService,
        retrieval: RetrievalService,
        user_id: str,
        other_user_id: str,
        scenario: dict[str, str],
        cleanup: None,
    ) -> None:
        """The critical isolation case for a shared-node graph.

        Another user asserts an edge from *their* entity to a node with the same
        normalized name. Because entity identity is per user, their node is distinct,
        and the path filter additionally requires every hop to belong to the
        requesting user -- so the traversal must not reach their data.
        """
        await memories.create(
            user_id=other_user_id,
            payload=rel_memory(
                "Their project uses EasyOCR",
                "Their Secret Project",
                "project",
                "uses",
                "EasyOCR",
                "technology",
            ),
        )
        result = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], hops=4
        )
        names = {n.entity.name for n in result.nodes}
        assert "Their Secret Project" not in names
        for node in result.nodes:
            assert node.entity.user_id == user_id
        for relationship in result.relationships:
            assert relationship.user_id == user_id

    async def test_cannot_supersede_another_users_memory(
        self, memories: MemoryService, user_id: str, other_user_id: str, cleanup: None
    ) -> None:
        theirs = await memories.create(
            user_id=other_user_id,
            payload=rel_memory("Theirs", "A", "project", "uses", "B", "technology"),
        )
        with pytest.raises(NotFoundError):
            await memories.create(
                user_id=user_id,
                payload=rel_memory(
                    "Mine",
                    "C",
                    "project",
                    "uses",
                    "D",
                    "technology",
                    supersedes=[theirs.memory.id],
                ),
            )
        untouched = await memories.get(user_id=other_user_id, memory_id=theirs.memory.id)
        assert untouched.memory.status is MemoryStatus.ACTIVE

    async def test_path_traversal_cannot_cross_into_another_users_subgraph(
        self,
        graph: GraphClient,
        memories: MemoryService,
        retrieval: RetrievalService,
        user_id: str,
        other_user_id: str,
        scenario: dict[str, str],
        cleanup: None,
    ) -> None:
        """PHASE4_PLAN.md sections 1/4: the path-capturing multi-hop method
        needs the exact same isolation guarantee as the pre-existing
        `get_neighbourhood` -- see `test_traversal_cannot_cross_into_another_users_subgraph`.
        """
        await memories.create(
            user_id=other_user_id,
            payload=rel_memory(
                "Their project uses EasyOCR",
                "Their Secret Project",
                "project",
                "uses",
                "EasyOCR",
                "technology",
            ),
        )
        result = await retrieval.get_neighbourhood_with_paths(
            user_id=user_id, entity_id=scenario["Drawing Extractor"], hops=3, max_nodes=200
        )
        names = {n.entity.name for n in result}
        assert "Their Secret Project" not in names
        for node in result:
            assert node.entity.user_id == user_id
            for step in node.path:
                assert step.entity_id != "Their Secret Project"

    async def test_resolve_cannot_resolve_another_users_entity(
        self,
        graph: GraphClient,
        user_id: str,
        other_user_id: str,
        scenario: dict[str, str],
        memories: MemoryService,
        cleanup: None,
    ) -> None:
        """PHASE4_PLAN.md section 9: the canonical resolver is user-scoped --
        asking to resolve an id that exists, but belongs to a different user,
        must behave exactly like asking about an id that does not exist.
        """
        theirs = await memories.create(
            user_id=other_user_id,
            payload=rel_memory("Theirs", "Their Entity", "project", "uses", "X", "technology"),
        )
        their_entity_id = (
            theirs.entities[0].id
            if theirs.entities[0].name == "Their Entity"
            else theirs.entities[1].id
        )

        entity_repo = EntityRepository(graph)
        resolved_as_owner = await entity_repo.resolve(user_id=user_id, entity_id=their_entity_id)
        assert resolved_as_owner is None, "must not resolve another user's entity"

    async def test_merge_cannot_merge_across_users(
        self,
        graph: GraphClient,
        user_id: str,
        other_user_id: str,
        scenario: dict[str, str],
        memories: MemoryService,
        cleanup: None,
    ) -> None:
        """PHASE4_PLAN.md section 9's highest-risk case: attempting to merge
        one user's entity with another user's entity must fail closed, never
        silently redirect across the isolation boundary.
        """

        theirs = await memories.create(
            user_id=other_user_id,
            payload=rel_memory("Theirs", "Their Entity", "project", "uses", "X", "technology"),
        )
        their_entity_id = (
            theirs.entities[0].id
            if theirs.entities[0].name == "Their Entity"
            else theirs.entities[1].id
        )

        entity_repo = EntityRepository(graph)
        with pytest.raises(ConflictError):
            # user_id owns Drawing Extractor; the "user_id" passed scopes the
            # whole call, so entity_id_b (their_entity_id) simply will not
            # match any node for this user -- the CREATE_MERGE_REDIRECT guard
            # finds zero rows, exactly like a nonexistent id.
            await entity_repo.merge(
                user_id=user_id,
                entity_id_a=scenario["Drawing Extractor"],
                entity_id_b=their_entity_id,
                merge_id="cross-user-merge-attempt",
                new_aliases=[],
                excluded_statuses=[],
                now=JAN,
                id_fn=lambda: "unused",
            )
        # Their entity must remain completely untouched.
        untouched = await entity_repo.resolve(user_id=other_user_id, entity_id=their_entity_id)
        assert untouched is not None
        assert untouched.was_redirected is False
