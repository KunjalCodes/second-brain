"""Repository layer: parameter passing and record mapping.

The GraphClient is replaced with a recorder, so the real repository code and the real
Cypher run through their normal path while the driver is stubbed. This proves that
`user_id` is actually *passed* for every operation -- the static audit in
test_cypher_safety.py proves the queries *reference* it; together they close the gap.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.core.errors import NotFoundError
from app.graph.connection import TxStep
from app.models.enums import Direction, MemoryStatus
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from tests.conftest import JAN, MAY

USER = "user-a"


class RecordingClient:
    """Records every query and parameter set, returning canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.transactions: list[list[tuple[str, dict[str, Any]]]] = []
        self.transaction_results: list[list[dict[str, Any]]] | None = None

    async def read(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((query, params or {}))
        return self.rows

    async def write(self, query: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((query, params or {}))
        return self.rows

    async def write_transaction(self, steps: Sequence[TxStep]) -> list[list[dict]]:
        """Record the steps and run each step's check, as a real transaction would.

        Invoking `check` here is what lets the unit tests exercise the rejection
        path; a double that skipped the checks would report success for a write the
        database would roll back.
        """
        self.transactions.append(list(steps))
        self.calls.extend((step.query, step.params) for step in steps)
        collected: list[list[dict[str, Any]]] = []
        for index, step in enumerate(steps):
            if self.transaction_results is not None:
                rows = (
                    self.transaction_results[index]
                    if index < len(self.transaction_results)
                    else []
                )
            else:
                rows = self.rows
            if step.check is not None:
                step.check(rows)
            collected.append(rows)
        return collected

    @property
    def all_params(self) -> list[dict[str, Any]]:
        return [params for _, params in self.calls]


def memory_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "m1",
        "user_id": USER,
        "content": "Drawing Extractor uses EasyOCR",
        "memory_type": "semantic",
        "status": "active",
        "confidence": 1.0,
        "created_at": JAN,
        "updated_at": JAN,
        "valid_from": JAN,
    }
    row.update(overrides)
    return row


def entity_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "e1",
        "user_id": USER,
        "name": "Drawing Extractor",
        "normalized_name": "drawing extractor",
        "entity_type": "project",
        "aliases": [],
        "created_at": JAN,
        "updated_at": JAN,
    }
    row.update(overrides)
    return row


def relationship_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "r1",
        "user_id": USER,
        "predicate": "uses",
        "source_entity_id": "e1",
        "target_entity_id": "e2",
        "status": "active",
        "confidence": 1.0,
        "memory_id": "m1",
        "valid_from": JAN,
        "created_at": JAN,
        "updated_at": JAN,
    }
    row.update(overrides)
    return row


class TestUserScoping:
    """Every operation must pass the user id through to the driver."""

    async def test_create_passes_user_id_in_every_step(self) -> None:
        client = RecordingClient()
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        await repo.create(
            user_id=USER,
            memory_props=memory_row(),
            entities=[
                {
                    "id": "e1",
                    "name": "Drawing Extractor",
                    "normalized_name": "drawing extractor",
                    "entity_type": "project",
                    "aliases": [],
                }
            ],
            relationships=[
                {
                    "source_normalized": "drawing extractor",
                    "target_normalized": "easyocr",
                    "props": {"id": "r1", "user_id": USER, "predicate": "uses"},
                }
            ],
            source={
                "id": "s1",
                "source_type": "conversation",
                "reference": "c1",
                "location": None,
                "dedup_key": "c1||",
            },
            supersedes=[],
            now=JAN,
        )
        assert len(client.transactions) == 1, "the write must be one transaction"
        for _, params in client.calls:
            assert params.get("user_id") == USER

    async def test_reads_pass_user_id(self) -> None:
        detail_rows = [
            {"memory": memory_row(), "entities": [], "sources": [],
             "relationships": [], "supersedes": [], "superseded_by": [],
             "conflicts_with": [], "duplicate_of": [], "duplicates": []}
        ]
        seen: list[dict[str, Any]] = []

        get_client = RecordingClient(detail_rows)
        await MemoryRepository(get_client).get(user_id=USER, memory_id="m1")  # type: ignore[arg-type]
        seen.extend(get_client.all_params)

        search_client = RecordingClient(detail_rows)
        await MemoryRepository(search_client).search(  # type: ignore[arg-type]
            user_id=USER, filters={}, limit=10, offset=0
        )
        seen.extend(search_client.all_params)

        count_client = RecordingClient([{"total": 3}])
        total = await MemoryRepository(count_client).count(  # type: ignore[arg-type]
            user_id=USER, filters={}
        )
        seen.extend(count_client.all_params)

        assert total == 3
        assert seen, "no queries were recorded"
        assert all(p.get("user_id") == USER for p in seen)

    async def test_entity_reads_pass_user_id(self) -> None:
        client = RecordingClient([{"entity": entity_row(), "memory_count": 0,
                                   "relationship_count": 0}])
        repo = EntityRepository(client)  # type: ignore[arg-type]
        await repo.get(user_id=USER, entity_id="e1")
        await repo.find_by_normalized_name(user_id=USER, normalized_name="x")
        await repo.search(user_id=USER, query="x", limit=5, offset=0)
        assert all(p.get("user_id") == USER for p in client.all_params)


class TestMemoryReads:
    async def test_get_returns_none_when_absent(self) -> None:
        repo = MemoryRepository(RecordingClient([]))  # type: ignore[arg-type]
        assert await repo.get(user_id=USER, memory_id="nope") is None

    async def test_get_returns_none_when_row_has_no_memory(self) -> None:
        """An OPTIONAL MATCH can yield a row whose memory column is null."""
        repo = MemoryRepository(RecordingClient([{"memory": None}]))  # type: ignore[arg-type]
        assert await repo.get(user_id=USER, memory_id="nope") is None

    async def test_get_maps_the_full_detail(self) -> None:
        client = RecordingClient(
            [
                {
                    "memory": memory_row(),
                    "entities": [entity_row(), None],
                    "sources": [
                        {
                            "id": "s1",
                            "user_id": USER,
                            "source_type": "conversation",
                            "reference": "conv-1",
                            "created_at": JAN,
                        },
                        None,
                    ],
                    "relationships": [relationship_row(), None],
                    "supersedes": ["m0", None],
                    "superseded_by": [None],
                    "conflicts_with": ["m2", None],
                    "duplicate_of": ["m3", None],
                    "duplicates": [None],
                }
            ]
        )
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        detail = await repo.get(user_id=USER, memory_id="m1")
        assert detail is not None
        assert detail.memory.id == "m1"
        # Nulls produced by OPTIONAL MATCH must be filtered out, not mapped.
        assert len(detail.entities) == 1
        assert len(detail.relationships) == 1
        assert detail.supersedes == ["m0"]
        assert detail.superseded_by == []
        assert detail.conflicts_with == ["m2"]
        assert detail.duplicate_of == ["m3"]
        assert detail.duplicates == []
        assert len(detail.sources) == 1
        assert detail.sources[0].reference == "conv-1"

    async def test_missing_optional_properties_become_none(self) -> None:
        """Neo4j omits null properties entirely; the mapper must tolerate that."""
        repo = MemoryRepository(
            RecordingClient(
                [{"memory": memory_row(), "entities": [], "sources": [],
                  "relationships": [], "supersedes": [], "superseded_by": [],
                  "conflicts_with": [], "duplicate_of": [], "duplicates": []}]
            )
        )  # type: ignore[arg-type]
        detail = await repo.get(user_id=USER, memory_id="m1")
        assert detail is not None
        assert detail.memory.valid_until is None
        assert detail.memory.invalidation_reason is None


class TestSupersession:
    async def test_rejects_unknown_supersession_target(self) -> None:
        """If a superseded id does not exist, the whole write must be rejected."""
        client = RecordingClient()
        # steps: create, supersede-memories, supersede-relationships
        client.transaction_results = [[], [{"superseded_ids": []}], [{"closed": 0}]]
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError) as exc:
            await repo.create(
                user_id=USER,
                memory_props=memory_row(),
                entities=[],
                relationships=[],
                source=None,
                supersedes=["ghost"],
                now=MAY,
            )
        assert exc.value.details["missing_memory_ids"] == ["ghost"]

    async def test_accepts_known_supersession_target(self) -> None:
        client = RecordingClient()
        client.transaction_results = [[], [{"superseded_ids": ["m0"]}], [{"closed": 1}]]
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        await repo.create(
            user_id=USER,
            memory_props=memory_row(),
            entities=[],
            relationships=[],
            source=None,
            supersedes=["m0"],
            now=MAY,
        )
        statuses = [
            p.get("superseded_status") for p in client.all_params if "superseded_status" in p
        ]
        assert statuses == [MemoryStatus.SUPERSEDED.value] * 2

    async def test_supersession_closes_the_asserted_edges_too(self) -> None:
        """Superseding a memory must also close the relationships it asserted."""
        client = RecordingClient()
        client.transaction_results = [[], [{"superseded_ids": ["m0"]}], [{"closed": 2}]]
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        await repo.create(
            user_id=USER,
            memory_props=memory_row(),
            entities=[],
            relationships=[],
            source=None,
            supersedes=["m0"],
            now=MAY,
        )
        queries = [step.query for step in client.transactions[0]]
        assert any("RELATES_TO" in q and "superseded_status" in q for q in queries)


class TestTransactionalRejection:
    """Regression guard: supersession validation must happen INSIDE the transaction.

    An earlier version validated after `write_transaction` returned, by which point
    the memory, entity and relationship writes had already committed -- so a
    supersession naming a nonexistent memory raised NotFoundError *and* left orphan
    nodes in the graph. Only a real database exposed that; the unit-level double had
    validated up front. These tests pin the mechanism that fixed it.
    """

    async def test_supersede_step_carries_an_in_transaction_check(self) -> None:
        client = RecordingClient()
        client.transaction_results = [[], [{"superseded_ids": ["m0"]}], [{"closed": 0}]]
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        await repo.create(
            user_id=USER,
            memory_props=memory_row(),
            entities=[],
            relationships=[],
            source=None,
            supersedes=["m0"],
            now=MAY,
        )
        steps = client.transactions[0]
        checked = [step for step in steps if step.check is not None]
        assert len(checked) == 1, (
            "exactly one step must validate the supersession, and it must do so "
            "inside the transaction so a failure rolls the write back"
        )
        assert "SUPERSEDES" in checked[0].query

    async def test_check_raises_before_later_steps_run(self) -> None:
        """A failing check must abort the transaction, not merely report afterwards."""
        client = RecordingClient()
        client.transaction_results = [[], [{"superseded_ids": []}], [{"closed": 0}]]
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await repo.create(
                user_id=USER,
                memory_props=memory_row(),
                entities=[],
                relationships=[],
                source=None,
                supersedes=["ghost"],
                now=MAY,
            )

    def test_every_write_path_goes_through_a_transaction(self) -> None:
        """create and invalidate must both be transactional, not loose writes."""
        import inspect

        for name in ("create", "invalidate"):
            source = inspect.getsource(getattr(MemoryRepository, name))
            assert "write_transaction" in source, name


class TestInvalidation:
    async def test_invalidation_runs_in_one_transaction(self) -> None:
        client = RecordingClient([{"memory": memory_row(status="invalid")}])
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        result = await repo.invalidate(
            user_id=USER, memory_id="m1", reason="user retracted", now=MAY
        )
        assert result is not None
        assert result.status is MemoryStatus.INVALID
        assert len(client.transactions) == 1
        assert len(client.transactions[0]) == 2, "memory and its edges together"

    async def test_invalidation_never_deletes(self) -> None:
        """Retraction must be recorded, not erased."""
        from app.repositories.memory_repository import (
            INVALIDATE_MEMORY,
            INVALIDATE_RELATIONSHIPS,
        )

        for query in (INVALIDATE_MEMORY, INVALIDATE_RELATIONSHIPS):
            assert "DELETE" not in query.upper()

    async def test_returns_none_when_memory_absent(self) -> None:
        client = RecordingClient([])
        repo = MemoryRepository(client)  # type: ignore[arg-type]
        assert await repo.invalidate(
            user_id=USER, memory_id="ghost", reason=None, now=MAY
        ) is None


class TestNoDestructiveStatements:
    #: PHASE4_PLAN.md section 10 / ADR 0010: reversing an entity merge deletes
    #: exactly the RELATES_TO copies that merge created (tagged
    #: `merged_via=$merge_id`) and the MERGED_INTO redirect itself -- never a
    #: Memory node, an Entity node, or an original user-asserted relationship.
    #: This is the one narrow, explicit exception to "memory is append-only":
    #: the deleted edges are merge-derived copies, not historical evidence, so
    #: removing them does not violate "never delete history".
    _ALLOWED_DELETE_STATEMENTS = frozenset({"REVERSE_MERGE_EDGES", "REVERSE_MERGE_REDIRECT"})

    def test_repository_contains_no_delete_statements(self) -> None:
        """Phase 1 memory is append-only: nothing may issue a DELETE."""
        from app.repositories import entity_repository, memory_repository

        for module in (memory_repository, entity_repository):
            for name, value in vars(module).items():
                if name.isupper() and isinstance(value, str):
                    if name in self._ALLOWED_DELETE_STATEMENTS:
                        continue
                    upper = value.upper()
                    assert "DETACH DELETE" not in upper, name
                    assert "DELETE " not in upper, name
                    assert "REMOVE " not in upper, name


class TestDirectionFiltering:
    async def test_outgoing_excludes_incoming_edges(self) -> None:
        client = RecordingClient(
            [
                {
                    "relationship": relationship_row(source_entity_id="e1",
                                                     target_entity_id="e2"),
                    "source_entity": entity_row(id="e1"),
                    "target_entity": entity_row(id="e2", name="EasyOCR"),
                },
                {
                    "relationship": relationship_row(id="r2", source_entity_id="e3",
                                                     target_entity_id="e1"),
                    "source_entity": entity_row(id="e3", name="Other"),
                    "target_entity": entity_row(id="e1"),
                },
            ]
        )
        repo = EntityRepository(client)  # type: ignore[arg-type]
        outgoing = await repo.relationships(
            user_id=USER, entity_id="e1", direction=Direction.OUTGOING,
            excluded_statuses=[],
        )
        incoming = await repo.relationships(
            user_id=USER, entity_id="e1", direction=Direction.INCOMING,
            excluded_statuses=[],
        )
        both = await repo.relationships(
            user_id=USER, entity_id="e1", direction=Direction.BOTH, excluded_statuses=[]
        )
        assert [v.relationship.id for v in outgoing] == ["r1"]
        assert [v.relationship.id for v in incoming] == ["r2"]
        assert len(both) == 2
