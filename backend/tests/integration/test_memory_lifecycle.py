"""Memory lifecycle against real Neo4j.

Mirrors tests/unit/test_memory_service.py, but exercises the actual Cypher: node
creation, entity MERGE, MENTIONS/DERIVED_FROM/SUPERSEDES edges, and the property
updates that close a validity window.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.graph.connection import GraphClient
from app.models.enums import MemoryStatus
from app.schemas.memory import MemoryCreate, MemoryUpdate
from app.services.memory_service import MemoryService
from tests.conftest import JAN, MAR, MAY, FrozenClock

pytestmark = pytest.mark.integration


def uses_payload(target: str = "EasyOCR", **overrides: object) -> MemoryCreate:
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
    async def test_persists_across_connections(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        """The point of the exercise: memory must survive, in the database."""
        created = await memories.create(user_id=user_id, payload=uses_payload())
        rows = await graph.read(
            "MATCH (m:Memory {id: $id, user_id: $user_id}) RETURN m.content AS content",
            {"id": created.memory.id, "user_id": user_id},
        )
        assert rows[0]["content"] == "Drawing Extractor uses EasyOCR"

    async def test_writes_the_expected_graph_shape(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(user_id=user_id, payload=uses_payload())
        rows = await graph.read(
            """
            MATCH (m:Memory {id: $id, user_id: $user_id})
            OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
            OPTIONAL MATCH (m)-[:DERIVED_FROM]->(s:Source)
            OPTIONAL MATCH (:Entity)-[r:RELATES_TO {memory_id: $id}]->(:Entity)
            RETURN count(DISTINCT e) AS entities,
                   count(DISTINCT s) AS sources,
                   count(DISTINCT r) AS relationships
            """,
            {"id": created.memory.id, "user_id": user_id},
        )
        assert rows[0] == {"entities": 2, "sources": 1, "relationships": 1}

    async def test_entity_is_merged_not_duplicated(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        await memories.create(user_id=user_id, payload=uses_payload("Gemini"))
        rows = await graph.read(
            "MATCH (e:Entity {user_id: $user_id, normalized_name: 'drawing extractor'}) "
            "RETURN count(e) AS n",
            {"user_id": user_id},
        )
        assert rows[0]["n"] == 1

    async def test_case_variants_merge_to_one_entity(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        await memories.create(user_id=user_id, payload=uses_payload("easyocr"))
        rows = await graph.read(
            "MATCH (e:Entity {user_id: $user_id, normalized_name: 'easyocr'}) "
            "RETURN count(e) AS n",
            {"user_id": user_id},
        )
        assert rows[0]["n"] == 1

    async def test_entity_type_is_not_overwritten_on_rematch(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        """First assertion wins; a later mention must not silently retype an entity."""
        await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        await memories.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "EasyOCR again",
                    "entities": [{"name": "EasyOCR", "entity_type": "concept"}],
                }
            ),
        )
        detail = await memories.search(user_id=user_id, entity_name="EasyOCR")
        assert detail.total == 2

    async def test_sources_are_shared_between_memories(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        """Two memories from one conversation must reference a single Source node."""
        await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        await memories.create(user_id=user_id, payload=uses_payload("Gemini"))
        rows = await graph.read(
            "MATCH (s:Source {user_id: $user_id}) RETURN count(s) AS n",
            {"user_id": user_id},
        )
        assert rows[0]["n"] == 1

    async def test_aliases_are_unioned_across_mentions(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        for aliases in (["easy ocr"], ["easy-ocr"]):
            await memories.create(
                user_id=user_id,
                payload=MemoryCreate.model_validate(
                    {
                        "content": "EasyOCR mention",
                        "entities": [
                            {
                                "name": "EasyOCR",
                                "entity_type": "technology",
                                "aliases": aliases,
                            }
                        ],
                    }
                ),
            )
        rows = await graph.read(
            "MATCH (e:Entity {user_id: $user_id, normalized_name: 'easyocr'}) "
            "RETURN e.aliases AS aliases",
            {"user_id": user_id},
        )
        assert set(rows[0]["aliases"]) == {"easy ocr", "easy-ocr"}


class TestAtomicity:
    async def test_rejected_supersession_writes_nothing(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        """The whole create must roll back, leaving no orphan nodes behind."""
        with pytest.raises(NotFoundError):
            await memories.create(
                user_id=user_id, payload=uses_payload(supersedes=["does-not-exist"])
            )
        rows = await graph.read(
            "MATCH (n {user_id: $user_id}) RETURN count(n) AS n", {"user_id": user_id}
        )
        assert rows[0]["n"] == 0, "a failed write left residue in the graph"


class TestSupersession:
    async def test_history_is_preserved_in_the_database(
        self, graph: GraphClient, memories: MemoryService, user_id: str,
        clock: FrozenClock, cleanup: None,
    ) -> None:
        old = await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        new = await memories.create(
            user_id=user_id,
            payload=uses_payload("PaddleOCR", supersedes=[old.memory.id]),
        )
        rows = await graph.read(
            """
            MATCH (m:Memory {id: $old_id, user_id: $user_id})
            OPTIONAL MATCH (newer:Memory)-[:SUPERSEDES]->(m)
            RETURN m.status AS status, m.valid_until AS valid_until,
                   m.content AS content, newer.id AS newer_id
            """,
            {"old_id": old.memory.id, "user_id": user_id},
        )
        row = rows[0]
        assert row["status"] == MemoryStatus.SUPERSEDED.value
        assert row["valid_until"] == MAY
        assert row["content"] == "Drawing Extractor uses EasyOCR"
        assert row["newer_id"] == new.memory.id

    async def test_superseded_edge_window_is_closed(
        self, graph: GraphClient, memories: MemoryService, user_id: str,
        clock: FrozenClock, cleanup: None,
    ) -> None:
        old = await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await memories.create(
            user_id=user_id,
            payload=uses_payload("PaddleOCR", supersedes=[old.memory.id]),
        )
        rows = await graph.read(
            "MATCH (:Entity)-[r:RELATES_TO {memory_id: $old_id, user_id: $user_id}]->"
            "(:Entity) RETURN r.status AS status, r.valid_from AS valid_from, "
            "r.valid_until AS valid_until",
            {"old_id": old.memory.id, "user_id": user_id},
        )
        assert rows[0]["status"] == MemoryStatus.SUPERSEDED.value
        assert rows[0]["valid_from"] == JAN
        assert rows[0]["valid_until"] == MAY

    async def test_nothing_is_ever_deleted(
        self, graph: GraphClient, memories: MemoryService, user_id: str,
        clock: FrozenClock, cleanup: None,
    ) -> None:
        old = await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await memories.create(
            user_id=user_id,
            payload=uses_payload("PaddleOCR", supersedes=[old.memory.id]),
        )
        rows = await graph.read(
            "MATCH (m:Memory {user_id: $user_id}) RETURN count(m) AS n",
            {"user_id": user_id},
        )
        assert rows[0]["n"] == 2, "the superseded memory must still exist"

    async def test_explicit_end_date_survives_supersession(
        self, memories: MemoryService, user_id: str, clock: FrozenClock, cleanup: None
    ) -> None:
        old = await memories.create(
            user_id=user_id, payload=uses_payload("EasyOCR", valid_until=MAR.isoformat())
        )
        clock.advance_to(MAY)
        await memories.create(
            user_id=user_id,
            payload=uses_payload("PaddleOCR", supersedes=[old.memory.id]),
        )
        reloaded = await memories.get(user_id=user_id, memory_id=old.memory.id)
        assert reloaded.memory.valid_until == MAR


class TestUpdate:
    async def test_metadata_update_persists(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(user_id=user_id, payload=uses_payload())
        await memories.update(
            user_id=user_id,
            memory_id=created.memory.id,
            payload=MemoryUpdate(confidence=0.42),
        )
        reloaded = await memories.get(user_id=user_id, memory_id=created.memory.id)
        assert reloaded.memory.confidence == 0.42

    async def test_clearing_valid_until_removes_the_property(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        """Neo4j stores no nulls: setting None must remove the property."""
        created = await memories.create(
            user_id=user_id, payload=uses_payload(valid_until=MAY.isoformat())
        )
        await memories.update(
            user_id=user_id,
            memory_id=created.memory.id,
            payload=MemoryUpdate(valid_until=None),
        )
        rows = await graph.read(
            "MATCH (m:Memory {id: $id, user_id: $user_id}) "
            "RETURN m.valid_until IS NULL AS cleared",
            {"id": created.memory.id, "user_id": user_id},
        )
        assert rows[0]["cleared"] is True

    async def test_illegal_transition_is_rejected(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(user_id=user_id, payload=uses_payload())
        await memories.invalidate(
            user_id=user_id, memory_id=created.memory.id, reason="wrong"
        )
        with pytest.raises(ConflictError):
            await memories.update(
                user_id=user_id,
                memory_id=created.memory.id,
                payload=MemoryUpdate(status=MemoryStatus.ACTIVE),
            )


class TestInvalidation:
    async def test_marks_invalid_without_deleting(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(user_id=user_id, payload=uses_payload())
        await memories.invalidate(
            user_id=user_id, memory_id=created.memory.id, reason="I misremembered"
        )
        rows = await graph.read(
            "MATCH (m:Memory {id: $id, user_id: $user_id}) "
            "RETURN m.status AS status, m.invalidation_reason AS reason",
            {"id": created.memory.id, "user_id": user_id},
        )
        assert rows[0]["status"] == MemoryStatus.INVALID.value
        assert rows[0]["reason"] == "I misremembered"

    async def test_closes_the_asserted_edges(
        self, graph: GraphClient, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(user_id=user_id, payload=uses_payload())
        await memories.invalidate(
            user_id=user_id, memory_id=created.memory.id, reason=None
        )
        rows = await graph.read(
            "MATCH (:Entity)-[r:RELATES_TO {memory_id: $id, user_id: $user_id}]->"
            "(:Entity) RETURN r.status AS status",
            {"id": created.memory.id, "user_id": user_id},
        )
        assert all(row["status"] == MemoryStatus.INVALID.value for row in rows)


class TestSearch:
    async def test_content_search_is_case_insensitive(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        assert (await memories.search(user_id=user_id, query="EASYOCR")).total == 1
        assert (await memories.search(user_id=user_id, query="easyocr")).total == 1

    async def test_as_of_filter_runs_in_cypher(
        self, memories: MemoryService, user_id: str, clock: FrozenClock, cleanup: None
    ) -> None:
        """The headline temporal query, evaluated server-side."""
        old = await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await memories.create(
            user_id=user_id,
            payload=uses_payload("PaddleOCR", supersedes=[old.memory.id]),
        )
        march = await memories.search(user_id=user_id, as_of=MAR)
        assert [m.content for m in march.items] == ["Drawing Extractor uses EasyOCR"]

    async def test_as_of_boundary_is_half_open_in_cypher_too(
        self, memories: MemoryService, user_id: str, clock: FrozenClock, cleanup: None
    ) -> None:
        """`app.models.lifecycle.is_valid_at` is exhaustively boundary-tested in
        Python (`tests/unit/test_lifecycle.py`), but the Cypher `as_of` fragment in
        `MemoryRepository.FILTER_FRAGMENTS` is a second, independent implementation
        of the exact same half-open-interval rule (docs/PHASE2_HARDENING.md section
        9: the duplication is real -- Cypher and Python cannot share code -- and is
        deliberately kept rather than moved to application-side filtering, which
        would break server-side pagination). This test is what would catch the two
        definitions drifting apart: it proves the Cypher side treats `valid_from`
        as inclusive and `valid_until` as exclusive at the *exact* supersession
        instant, not just at a comfortable midpoint like `test_as_of_filter_runs_in_cypher`.
        """
        old = await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        clock.advance_to(MAY)
        await memories.create(
            user_id=user_id,
            payload=uses_payload("PaddleOCR", supersedes=[old.memory.id]),
        )

        at_start = await memories.search(user_id=user_id, as_of=JAN)
        assert [m.content for m in at_start.items] == ["Drawing Extractor uses EasyOCR"]

        # The exact supersession instant: the old window's end (exclusive) and the
        # new window's start (inclusive) are the same timestamp. Half-open means
        # exactly one of the two must be visible here -- never both, never neither.
        at_boundary = await memories.search(user_id=user_id, as_of=MAY)
        assert [m.content for m in at_boundary.items] == ["Drawing Extractor uses PaddleOCR"]

    async def test_invalid_memories_hidden_by_default(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        created = await memories.create(user_id=user_id, payload=uses_payload())
        await memories.invalidate(
            user_id=user_id, memory_id=created.memory.id, reason=None
        )
        assert (await memories.search(user_id=user_id)).total == 0
        assert (
            await memories.search(user_id=user_id, include_excluded=True)
        ).total == 1

    async def test_entity_filter(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        await memories.create(user_id=user_id, payload=uses_payload("EasyOCR"))
        await memories.create(user_id=user_id, payload=uses_payload("Gemini"))
        assert (await memories.search(user_id=user_id, entity_name="Gemini")).total == 1

    async def test_pagination_is_stable(
        self, memories: MemoryService, user_id: str, cleanup: None
    ) -> None:
        for name in ("EasyOCR", "Gemini", "OpenCV", "PaddleOCR"):
            await memories.create(user_id=user_id, payload=uses_payload(name))
        first = await memories.search(user_id=user_id, limit=2, offset=0)
        second = await memories.search(user_id=user_id, limit=2, offset=2)
        assert first.total == 4
        ids = [m.id for m in first.items] + [m.id for m in second.items]
        assert len(set(ids)) == 4, "pages must not overlap"
