"""Memory graph exploration API against real Neo4j (and PostgreSQL for
evidence): temporal scopes and boundaries, merge redirects, aggregation,
truncation exactness, incremental expansion, evidence, timeline, D2 search,
and cross-user isolation on every id-accepting parameter (docs/PHASE8_PLAN.md
sections 13, 14, 15, 19).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.db.models import DocumentChunk as ChunkRow
from app.graph.connection import GraphClient
from app.models.conversation import ExtractionStatus, MessageRole
from app.models.document import format_chunk_source_location
from app.models.enums import MemoryStatus, SourceType
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.document_repository import DocumentChunkRepository, DocumentRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.graph_repository import GraphRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.schemas.graph import (
    EdgeKind,
    EvidenceStatus,
    GraphInclude,
    GraphSlice,
    NodeKind,
    TemporalScope,
    TemporalState,
)
from app.schemas.memory import MemoryCreate, MemoryUpdate
from app.services.graph_exploration_service import GraphExplorationService
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService
from app.services.retrieval_service import DEFAULT_EXCLUDED, RetrievalService
from tests.conftest import JAN, JUN, MAR, MAY, FrozenClock

pytestmark = pytest.mark.integration


def _rel(content: str, subject: str, predicate: str, obj: str, **kw: object) -> MemoryCreate:
    types = {
        "Drawing Extractor": "project",
        "Drawing Extraction Engine": "project",
        "OCR": "concept",
    }
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [
                {"name": subject, "entity_type": types.get(subject, "technology")},
                {"name": obj, "entity_type": types.get(obj, "technology")},
            ],
            "relationships": [{"source": subject, "predicate": predicate, "target": obj}],
            **kw,
        }
    )


def _bare(content: str, **kw: object) -> MemoryCreate:
    return MemoryCreate.model_validate(
        {
            "content": content,
            "entities": [{"name": "Drawing Extractor", "entity_type": "project"}],
            **kw,
        }
    )


def _service(
    graph: GraphClient, clock: FrozenClock, *, session: Any = None, **overrides: int
) -> GraphExplorationService:
    memory = MemoryService(MemoryRepository(graph), now_fn=clock)
    return GraphExplorationService(
        repository=GraphRepository(graph),
        retrieval=RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock),
        memory=memory,
        provenance=ProvenanceService(session, memory) if session is not None else None,
        settings=Settings(**overrides),
        now_fn=clock,
    )


def _entity_names(s: GraphSlice) -> set[str]:
    return {n.label for n in s.nodes if n.kind is NodeKind.ENTITY}


def _triples(s: GraphSlice, names: dict[str, str]) -> set[tuple[str, str, str]]:
    by_id = {v: k for k, v in names.items()}
    return {
        (by_id.get(e.source, e.source), e.predicate or "", by_id.get(e.target, e.target))
        for e in s.edges
        if e.kind is EdgeKind.RELATES_TO
    }


@pytest.fixture
async def world(
    graph: GraphClient, clock: FrozenClock, user_id: str, cleanup: None
) -> dict[str, Any]:
    """The EasyOCR -> PaddleOCR narrative plus every case the explorer must
    get right: a re-asserted fact, a confirmed duplicate, a conflict, a bare
    statement, and a merged-away entity."""
    memories = MemoryService(MemoryRepository(graph), now_fn=clock)
    retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
    m: dict[str, str] = {}

    clock.advance_to(JAN)
    for key, payload in {
        "easy": _rel("Drawing Extractor uses EasyOCR", "Drawing Extractor", "uses", "EasyOCR"),
        "gem1": _rel("Drawing Extractor uses Gemini", "Drawing Extractor", "uses", "Gemini"),
        "gem2": _rel("DE relies on Gemini", "Drawing Extractor", "uses", "Gemini"),
        "gem_dup": _rel("The extractor uses Gemini", "Drawing Extractor", "uses", "Gemini"),
        "ocr": _rel("EasyOCR is an OCR library", "EasyOCR", "related_to", "OCR"),
        "engine": _rel(
            "Engine depends on Python", "Drawing Extraction Engine", "depends_on", "Python"
        ),
        "bare": _bare("Drawing Extractor is my favourite project"),
        "deadline_a": _bare("Drawing Extractor deadline is March"),
        "deadline_b": _bare("Drawing Extractor deadline is April", status="conflicting"),
    }.items():
        m[key] = (await memories.create(user_id=user_id, payload=payload)).memory.id
    await memories.link_conflicting(
        user_id=user_id, memory_id_a=m["deadline_a"], memory_id_b=m["deadline_b"]
    )
    await memories.update(
        user_id=user_id,
        memory_id=m["deadline_a"],
        payload=MemoryUpdate(status=MemoryStatus.CONFLICTING),
    )
    await MemoryRepository(graph).confirm_duplicate(
        user_id=user_id, duplicate_id=m["gem_dup"], canonical_id=m["gem1"], now=JAN
    )

    clock.advance_to(MAY)
    m["paddle"] = (
        await memories.create(
            user_id=user_id,
            payload=_rel(
                "Drawing Extractor uses PaddleOCR",
                "Drawing Extractor",
                "uses",
                "PaddleOCR",
                supersedes=[m["easy"]],
            ),
        )
    ).memory.id

    names: dict[str, str] = {}
    for name in (
        "Drawing Extractor",
        "EasyOCR",
        "Gemini",
        "PaddleOCR",
        "OCR",
        "Drawing Extraction Engine",
        "Python",
    ):
        names[name] = (await retrieval.find_entity_by_name(user_id=user_id, name=name)).id
    # find_entity_by_name resolves redirects, so look the engine up before merging.
    await EntityRepository(graph).merge(
        user_id=user_id,
        entity_id_a=names["Drawing Extractor"],
        entity_id_b=names["Drawing Extraction Engine"],
        merge_id=str(uuid.uuid4()),
        new_aliases=["Drawing Extraction Engine"],
        excluded_statuses=DEFAULT_EXCLUDED,
        now=MAY,
        id_fn=lambda: str(uuid.uuid4()),
    )
    clock.advance_to(JUN)
    return {"m": m, "e": names}


# -- Temporal scopes ------------------------------------------------------------


class TestTemporalScopes:
    async def test_current_is_the_default_and_hides_superseded_facts(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=world["e"]["Drawing Extractor"]
        )
        assert s.temporal_scope is TemporalScope.CURRENT
        assert s.as_of == JUN
        assert _entity_names(s) == {"Drawing Extractor", "Gemini", "PaddleOCR", "Python"}
        assert all(e.temporal_state is TemporalState.CURRENT for e in s.edges)

    async def test_historical_edges_are_never_traversed_in_current_scope(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=world["e"]["Drawing Extractor"], hops=2
        )
        # OCR is only reachable through the superseded EasyOCR edge.
        assert "OCR" not in _entity_names(s)
        assert "EasyOCR" not in _entity_names(s)

    async def test_as_of_march_shows_what_was_true_then(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            temporal_scope=TemporalScope.AS_OF,
            as_of=MAR,
        )
        assert s.as_of == MAR
        assert _entity_names(s) == {"Drawing Extractor", "EasyOCR", "Gemini", "Python"}
        easy = next(e for e in s.edges if e.target == world["e"]["EasyOCR"])
        # It was true then; it has since been superseded -- status says so.
        assert easy.status is MemoryStatus.SUPERSEDED
        assert easy.temporal_state is TemporalState.CURRENT

    async def test_as_of_boundary_is_half_open(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        service = _service(graph, clock)
        de = world["e"]["Drawing Extractor"]
        at_boundary = await service.neighborhood(
            user_id=user_id, entity_id=de, temporal_scope=TemporalScope.AS_OF, as_of=MAY
        )
        # EasyOCR's window is [JAN, MAY): excluded AT MAY. PaddleOCR's is [MAY, ...).
        assert "EasyOCR" not in _entity_names(at_boundary)
        assert "PaddleOCR" in _entity_names(at_boundary)
        just_before = await service.neighborhood(
            user_id=user_id,
            entity_id=de,
            temporal_scope=TemporalScope.AS_OF,
            as_of=MAY - timedelta(microseconds=1),
        )
        assert "EasyOCR" in _entity_names(just_before)
        assert "PaddleOCR" not in _entity_names(just_before)

    async def test_all_scope_labels_every_edge(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            hops=2,
            temporal_scope=TemporalScope.ALL,
        )
        assert {"EasyOCR", "PaddleOCR", "OCR"} <= _entity_names(s)
        states = {e.target: e.temporal_state for e in s.edges if e.predicate == "uses"}
        assert states[world["e"]["EasyOCR"]] is TemporalState.HISTORICAL
        assert states[world["e"]["PaddleOCR"]] is TemporalState.CURRENT

    async def test_all_scope_labels_relative_to_an_explicit_instant(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            temporal_scope=TemporalScope.ALL,
            as_of=MAR,
        )
        states = {e.target: e.temporal_state for e in s.edges if e.predicate == "uses"}
        assert states[world["e"]["EasyOCR"]] is TemporalState.CURRENT
        assert states[world["e"]["PaddleOCR"]] is TemporalState.NOT_YET_VALID

    async def test_as_of_scope_requires_an_instant(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        with pytest.raises(ValidationError):
            await _service(graph, clock).neighborhood(
                user_id=user_id,
                entity_id=world["e"]["Drawing Extractor"],
                temporal_scope=TemporalScope.AS_OF,
            )

    async def test_current_scope_rejects_an_explicit_instant(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        with pytest.raises(ValidationError):
            await _service(graph, clock).neighborhood(
                user_id=user_id, entity_id=world["e"]["Drawing Extractor"], as_of=MAR
            )

    @pytest.mark.parametrize("at", [JAN, MAR, MAY - timedelta(microseconds=1), MAY, JUN])
    async def test_as_of_agrees_with_get_entity_state(
        self,
        graph,
        clock,
        user_id,
        world,
        at,  # noqa: ANN001
    ) -> None:
        """The slice's one-hop RELATES_TO facts at T are exactly
        `get_entity_state(as_of=T).current` -- the existing, tested answer to
        "what was I using in March?"."""
        de = world["e"]["Drawing Extractor"]
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
        state = await retrieval.get_entity_state(user_id=user_id, entity_id=de, as_of=at)
        expected = {
            (
                v.relationship.source_entity_id,
                v.relationship.predicate,
                v.relationship.target_entity_id,
            )
            for v in state.current
        }
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=de, temporal_scope=TemporalScope.AS_OF, as_of=at
        )
        got = {(e.source, e.predicate, e.target) for e in s.edges if de in (e.source, e.target)}
        assert got == expected

    async def test_current_agrees_with_get_entity_state_now(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        de = world["e"]["Drawing Extractor"]
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
        state = await retrieval.get_entity_state(user_id=user_id, entity_id=de)
        current = {
            (
                v.relationship.source_entity_id,
                v.relationship.predicate,
                v.relationship.target_entity_id,
            )
            for v in state.current
        }
        historical = {
            (
                v.relationship.source_entity_id,
                v.relationship.predicate,
                v.relationship.target_entity_id,
            )
            for v in state.historical
        }
        service = _service(graph, clock)
        now_slice = await service.neighborhood(user_id=user_id, entity_id=de)
        assert {(e.source, e.predicate, e.target) for e in now_slice.edges} == current
        all_slice = await service.neighborhood(
            user_id=user_id, entity_id=de, temporal_scope=TemporalScope.ALL
        )
        assert {
            (e.source, e.predicate, e.target)
            for e in all_slice.edges
            if e.temporal_state is TemporalState.HISTORICAL
        } == historical

    async def test_cypher_temporal_state_matches_the_python_classifier(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        from app.schemas.graph import temporal_state

        for at in (JAN, MAR, MAY, JUN):
            s = await _service(graph, clock).neighborhood(
                user_id=user_id,
                entity_id=world["e"]["Drawing Extractor"],
                temporal_scope=TemporalScope.ALL,
                as_of=at,
            )
            for e in s.edges:
                assert e.temporal_state is temporal_state(e.valid_from, e.valid_until, at)


# -- Aggregation, duplicates, conflicts, memories -----------------------------------


class TestAggregationAndMemories:
    async def test_two_assertions_collapse_into_one_edge_without_the_merged_duplicate(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=world["e"]["Drawing Extractor"]
        )
        gemini = [e for e in s.edges if e.target == world["e"]["Gemini"]]
        assert len(gemini) == 1
        assert gemini[0].assertion_count == 2
        assert set(gemini[0].memory_ids or []) == {world["m"]["gem1"], world["m"]["gem2"]}
        assert world["m"]["gem_dup"] not in (gemini[0].memory_ids or [])

    async def test_bare_statement_and_conflict_appear_only_with_include_memories(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        service = _service(graph, clock, graph_explore_max_memories_per_entity=25)
        de = world["e"]["Drawing Extractor"]
        plain = await service.neighborhood(user_id=user_id, entity_id=de)
        assert not [n for n in plain.nodes if n.kind is NodeKind.MEMORY]

        s = await service.neighborhood(
            user_id=user_id, entity_id=de, include={GraphInclude.MEMORIES}, max_nodes=100
        )
        memory_ids = {n.id for n in s.nodes if n.kind is NodeKind.MEMORY}
        assert world["m"]["bare"] in memory_ids
        assert {world["m"]["deadline_a"], world["m"]["deadline_b"]} <= memory_ids
        assert world["m"]["gem_dup"] not in memory_ids  # MERGED is excluded by default
        assert world["m"]["easy"] not in memory_ids  # superseded, not current
        conflicts = {
            frozenset((e.source, e.target)) for e in s.edges if e.kind is EdgeKind.CONFLICTS_WITH
        }
        assert frozenset((world["m"]["deadline_a"], world["m"]["deadline_b"])) in conflicts
        statuses = {n.id: n.status for n in s.nodes if n.kind is NodeKind.MEMORY}
        assert statuses[world["m"]["deadline_a"]] is MemoryStatus.CONFLICTING
        assert statuses[world["m"]["deadline_b"]] is MemoryStatus.CONFLICTING
        mentions = {(e.source, e.target) for e in s.edges if e.kind is EdgeKind.MENTIONS}
        assert (world["m"]["bare"], de) in mentions

    async def test_supersedes_link_is_drawn_in_all_scope(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            temporal_scope=TemporalScope.ALL,
            include={GraphInclude.MEMORIES},
            max_nodes=100,
        )
        supersedes = {(e.source, e.target) for e in s.edges if e.kind is EdgeKind.SUPERSEDES}
        assert (world["m"]["paddle"], world["m"]["easy"]) in supersedes

    async def test_memory_cap_per_entity_is_enforced_and_reported(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock, graph_explore_max_memories_per_entity=1).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            include={GraphInclude.MEMORIES},
        )
        mentioned: dict[str, int] = {}
        for e in s.edges:
            if e.kind is EdgeKind.MENTIONS:
                mentioned[e.target] = mentioned.get(e.target, 0) + 1
        assert max(mentioned.values()) <= 1
        assert s.truncated_memories is True
        assert s.budget.max_memories_per_entity == 1


# -- Entity resolution --------------------------------------------------------------


class TestMergedEntities:
    async def test_merged_id_resolves_to_canonical_with_redirect_info(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        engine = world["e"]["Drawing Extraction Engine"]
        s = await _service(graph, clock).neighborhood(user_id=user_id, entity_id=engine)
        assert s.root == world["e"]["Drawing Extractor"]
        assert s.resolved_from == engine
        assert s.redirect_depth == 1
        assert "Python" in _entity_names(s)  # the merge-copied edge

    async def test_merged_away_entity_never_appears_in_a_slice(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        engine = world["e"]["Drawing Extraction Engine"]
        service = _service(graph, clock)
        python_side = await service.neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Python"],
            hops=2,
            temporal_scope=TemporalScope.ALL,
        )
        assert engine not in {n.id for n in python_side.nodes}
        assert all(engine not in (e.source, e.target) for e in python_side.edges)
        overview = await service.overview(user_id=user_id, temporal_scope=TemporalScope.ALL)
        assert engine not in {n.id for n in overview.nodes}

    async def test_include_merges_reports_merged_from_count(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            include={GraphInclude.MERGES},
        )
        counts = {n.id: n.merged_from_count for n in s.nodes}
        assert counts[world["e"]["Drawing Extractor"]] == 1
        assert counts[world["e"]["Gemini"]] == 0

    async def test_redirect_cycle_does_not_loop(
        self,
        graph,
        clock,
        user_id,
        cleanup,  # noqa: ANN001
    ) -> None:
        """A (corrupt) MERGED_INTO cycle resolves safely via the existing resolver."""
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await graph.write(
            """
            CREATE (x:Entity {id: $a, user_id: $u, name: 'Cycle A', normalized_name: 'cycle a',
                              entity_type: 'concept', aliases: [], created_at: $t, updated_at: $t})
            CREATE (y:Entity {id: $b, user_id: $u, name: 'Cycle B', normalized_name: 'cycle b',
                              entity_type: 'concept', aliases: [], created_at: $t, updated_at: $t})
            CREATE (x)-[:MERGED_INTO {user_id: $u}]->(y)
            CREATE (y)-[:MERGED_INTO {user_id: $u}]->(x)
            """,
            {"a": a, "b": b, "u": user_id, "t": JAN},
        )
        s = await _service(graph, clock).neighborhood(user_id=user_id, entity_id=a)
        assert s.root == a  # no canonical end exists; resolves to itself


# -- Budgets, truncation, incremental expansion, determinism ------------------------


@pytest.fixture
async def hub(graph: GraphClient, clock: FrozenClock, user_id: str, cleanup: None) -> dict:
    memories = MemoryService(MemoryRepository(graph), now_fn=clock)
    clock.advance_to(JAN)
    for i in range(12):
        await memories.create(
            user_id=user_id, payload=_rel(f"Hub uses Leaf {i:02d}", "Hub", "uses", f"Leaf {i:02d}")
        )
    await memories.create(
        user_id=user_id, payload=_rel("Leaf 00 uses Twig", "Leaf 00", "uses", "Twig")
    )
    retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
    hub_entity = await retrieval.find_entity_by_name(user_id=user_id, name="Hub")
    clock.advance_to(JUN)
    return {"hub": hub_entity.id}


class TestBudgets:
    @pytest.mark.parametrize(
        ("max_nodes", "expected_nodes", "truncated"),
        [(5, 5, True), (12, 12, True), (13, 13, False), (100, 13, False)],
    )
    async def test_node_truncation_is_exact(
        self,
        graph,
        clock,
        user_id,
        hub,
        max_nodes,
        expected_nodes,
        truncated,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=hub["hub"], max_nodes=max_nodes
        )
        assert len(s.nodes) == expected_nodes
        assert s.truncated_nodes is truncated
        assert s.budget.max_nodes == max_nodes

    async def test_truncated_set_is_deterministic_by_name(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=hub["hub"], max_nodes=4
        )
        assert [n.label for n in s.nodes] == ["Hub", "Leaf 00", "Leaf 01", "Leaf 02"]

    async def test_budget_exactly_filled_at_a_hop_boundary_probes_the_next_hop(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        # 13 = root + 12 leaves fills the budget exactly; Twig is at hop 2.
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=hub["hub"], hops=2, max_nodes=13
        )
        assert len(s.nodes) == 13
        assert s.truncated_nodes is True
        complete = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=hub["hub"], hops=2, max_nodes=14
        )
        assert complete.truncated_nodes is False
        assert "Twig" in _entity_names(complete)
        assert {n.label: n.hops for n in complete.nodes}["Twig"] == 2

    @pytest.mark.parametrize(("max_edges", "truncated"), [(3, True), (12, False), (11, True)])
    async def test_edge_truncation_is_exact(
        self,
        graph,
        clock,
        user_id,
        hub,
        max_edges,
        truncated,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).neighborhood(
            user_id=user_id, entity_id=hub["hub"], max_edges=max_edges
        )
        assert len(s.edges) == min(max_edges, 12)
        assert s.truncated_edges is truncated

    async def test_client_cannot_raise_a_budget_past_the_server_cap(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock, graph_explore_max_nodes=3).neighborhood(
            user_id=user_id, entity_id=hub["hub"], max_nodes=1000
        )
        assert s.budget.max_nodes == 3
        assert len(s.nodes) == 3

    async def test_hops_beyond_the_cap_are_rejected(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        with pytest.raises(ValidationError):
            await _service(graph, clock).neighborhood(user_id=user_id, entity_id=hub["hub"], hops=3)

    async def test_exclude_ids_returns_only_new_nodes_plus_connecting_edges(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        service = _service(graph, clock)
        first = await service.neighborhood(user_id=user_id, entity_id=hub["hub"], max_nodes=4)
        loaded = [n.id for n in first.nodes]
        leaf00 = next(n.id for n in first.nodes if n.label == "Leaf 00")
        second = await service.neighborhood(user_id=user_id, entity_id=leaf00, exclude_ids=loaded)
        new = {n.label for n in second.nodes} - {"Leaf 00"}
        assert new == {"Twig"}
        # Leaf 00's edge back into the already-loaded Hub is returned to connect them.
        assert any(e.source == hub["hub"] and e.target == leaf00 for e in second.edges)

    async def test_repeated_requests_are_identical(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        service = _service(graph, clock)
        a = await service.neighborhood(user_id=user_id, entity_id=hub["hub"], hops=2, max_nodes=7)
        b = await service.neighborhood(user_id=user_id, entity_id=hub["hub"], hops=2, max_nodes=7)
        assert a.model_dump_json() == b.model_dump_json()

    async def test_predicate_and_entity_type_filters(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        service = _service(graph, clock)
        de = world["e"]["Drawing Extractor"]
        s = await service.neighborhood(user_id=user_id, entity_id=de, predicates=["depends_on"])
        assert _entity_names(s) == {"Drawing Extractor", "Python"}
        s = await service.neighborhood(user_id=user_id, entity_id=de, entity_types=["concept"])
        assert _entity_names(s) == {"Drawing Extractor"}


class TestOverview:
    async def test_most_recently_updated_first_then_name_and_bounded(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        # Every hub-fixture entity was last updated at JAN: ties break by name.
        s = await _service(graph, clock).overview(user_id=user_id, limit=3)
        assert [n.label for n in s.nodes] == ["Hub", "Leaf 00", "Leaf 01"]
        assert s.truncated_nodes is True
        assert s.root is None
        assert {(e.source, e.target) for e in s.edges} <= {
            (a.id, b.id) for a in s.nodes for b in s.nodes
        }

    async def test_current_by_default_and_complete_when_small(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).overview(user_id=user_id)
        assert s.truncated_nodes is False
        names = _entity_names(s)
        assert "Drawing Extraction Engine" not in names
        # Every edge is current; the superseded EasyOCR edge is not drawn.
        assert all(e.temporal_state is TemporalState.CURRENT for e in s.edges)
        assert world["e"]["EasyOCR"] not in {
            e.target for e in s.edges if e.source == world["e"]["Drawing Extractor"]
        }

    async def test_recency_ordering(
        self,
        graph,
        clock,
        user_id,
        hub,  # noqa: ANN001
    ) -> None:
        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        clock.advance_to(MAY)
        await memories.create(
            user_id=user_id, payload=_rel("Zed uses Leaf 07", "Zed", "uses", "Leaf 07")
        )
        s = await _service(graph, clock).overview(user_id=user_id, limit=3)
        # Zed is new and Leaf 07 was touched (updated_at=MAY) by that write.
        assert [n.label for n in s.nodes] == ["Leaf 07", "Zed", "Hub"]

    async def test_empty_for_a_user_with_no_graph(
        self,
        graph,
        clock,
        other_user_id,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).overview(user_id=other_user_id)
        assert s.nodes == [] and s.edges == [] and s.truncated_nodes is False


# -- Timeline ----------------------------------------------------------------------------


class TestTimeline:
    async def test_supersession_chain_is_chronological_with_distinct_statuses(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).memory_timeline(
            user_id=user_id, memory_id=world["m"]["paddle"]
        )
        assert [n.id for n in s.nodes] == [world["m"]["easy"], world["m"]["paddle"]]
        assert [n.status for n in s.nodes] == [MemoryStatus.SUPERSEDED, MemoryStatus.ACTIVE]
        assert [n.temporal_state for n in s.nodes] == [
            TemporalState.HISTORICAL,
            TemporalState.CURRENT,
        ]
        assert [(e.kind, e.source, e.target) for e in s.edges] == [
            (EdgeKind.SUPERSEDES, world["m"]["paddle"], world["m"]["easy"])
        ]
        assert s.temporal_scope is TemporalScope.ALL
        assert s.truncated_nodes is False

    async def test_duplicate_shows_merged_status_and_link(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).memory_timeline(
            user_id=user_id, memory_id=world["m"]["gem1"]
        )
        statuses = {n.id: n.status for n in s.nodes}
        assert statuses == {
            world["m"]["gem1"]: MemoryStatus.ACTIVE,
            world["m"]["gem_dup"]: MemoryStatus.MERGED,
        }
        assert [(e.kind, e.source, e.target) for e in s.edges] == [
            (EdgeKind.DUPLICATE_OF, world["m"]["gem_dup"], world["m"]["gem1"])
        ]

    async def test_conflict_is_followed_and_not_collapsed(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        s = await _service(graph, clock).memory_timeline(
            user_id=user_id, memory_id=world["m"]["deadline_a"]
        )
        assert {n.id: n.status for n in s.nodes} == {
            world["m"]["deadline_a"]: MemoryStatus.CONFLICTING,
            world["m"]["deadline_b"]: MemoryStatus.CONFLICTING,
        }
        assert [e.kind for e in s.edges] == [EdgeKind.CONFLICTS_WITH]

    async def test_invalid_and_historical_statuses_are_reported_as_stored(
        self,
        graph,
        clock,
        user_id,
        cleanup,  # noqa: ANN001
    ) -> None:
        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        clock.advance_to(JAN)
        old = await memories.create(
            user_id=user_id, payload=_bare("old fact", status="historical", valid_until=MAR)
        )
        clock.advance_to(MAY)
        new = await memories.create(user_id=user_id, payload=_bare("new fact"))
        await memories.link_conflicting(
            user_id=user_id, memory_id_a=old.memory.id, memory_id_b=new.memory.id
        )
        await memories.invalidate(user_id=user_id, memory_id=new.memory.id, reason="wrong")
        s = await _service(graph, clock).memory_timeline(user_id=user_id, memory_id=new.memory.id)
        assert [n.status for n in s.nodes] == [MemoryStatus.HISTORICAL, MemoryStatus.INVALID]
        assert [n.temporal_state for n in s.nodes][0] is TemporalState.HISTORICAL

    async def test_superseding_a_historical_memory_is_shown_as_superseded(
        self,
        graph,
        clock,
        user_id,
        cleanup,  # noqa: ANN001
    ) -> None:
        """Supersession re-files the old memory as SUPERSEDED (the stored
        lifecycle); the timeline reports that, not a re-derived label."""
        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        clock.advance_to(JAN)
        old = await memories.create(
            user_id=user_id, payload=_bare("old fact", status="historical", valid_until=MAR)
        )
        clock.advance_to(MAY)
        new = await memories.create(
            user_id=user_id, payload=_bare("new fact", supersedes=[old.memory.id])
        )
        s = await _service(graph, clock).memory_timeline(user_id=user_id, memory_id=old.memory.id)
        assert [n.status for n in s.nodes] == [MemoryStatus.SUPERSEDED, MemoryStatus.ACTIVE]
        assert [n.id for n in s.nodes] == [old.memory.id, new.memory.id]

    async def test_long_chain_is_bounded_and_flagged(
        self,
        graph,
        clock,
        user_id,
        cleanup,  # noqa: ANN001
    ) -> None:
        from app.services.graph_exploration_service import TIMELINE_MAX_CHAIN

        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        previous = None
        ids = []
        for i in range(TIMELINE_MAX_CHAIN + 3):
            clock.advance_to(JAN + timedelta(days=i))
            created = await memories.create(
                user_id=user_id,
                payload=_bare(f"step {i}", supersedes=[previous] if previous else []),
            )
            previous = created.memory.id
            ids.append(previous)
        s = await _service(graph, clock).memory_timeline(user_id=user_id, memory_id=ids[-1])
        assert len(s.nodes) == TIMELINE_MAX_CHAIN
        assert s.truncated_nodes is True
        # Chronological: oldest first.
        vfs = [n.valid_from for n in s.nodes]
        assert vfs == sorted(vfs)


# -- Evidence (Neo4j + PostgreSQL) -------------------------------------------------------


@pytest.fixture
async def evidence_world(
    db,
    graph: GraphClient,
    clock: FrozenClock,
    user_id: str,
    cleanup: None,
    pg_cleanup: None,  # noqa: ANN001
) -> dict[str, Any]:
    clock.advance_to(JAN)
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=JAN)
        conv = await ConversationRepository(session, user_id=user_id).create(
            conversation_id=str(uuid.uuid4()), title="t", now=JAN
        )
        msgs = []
        for text in ("We use Gemini for classification.", "Gemini is still our model."):
            msgs.append(
                await MessageRepository(session, user_id=user_id).append(
                    message_id=str(uuid.uuid4()),
                    conversation_id=conv.id,
                    role=MessageRole.USER,
                    content=text,
                    token_count=None,
                    now=JAN,
                )
            )
        document, _ = await DocumentRepository(session, user_id=user_id).create(
            document_id=str(uuid.uuid4()),
            filename="design.md",
            content_type="text/markdown",
            size_bytes=10,
            content_hash=str(uuid.uuid4()),
            storage_key=str(uuid.uuid4()),
            now=JAN,
        )
        chunk = ChunkRow(
            id=str(uuid.uuid4()),
            document_id=document.id,
            user_id=user_id,
            ordinal=0,
            content="The extractor calls Gemini.",
            content_hash="x",
            page_start=3,
            page_end=3,
            heading_context="Architecture",
            char_start=0,
            char_end=27,
            extraction_status=ExtractionStatus.DONE.value,
            created_at=JAN,
        )
        await DocumentChunkRepository(session, user_id=user_id).bulk_create(chunks=[chunk])

    memories = MemoryService(MemoryRepository(graph), now_fn=clock)
    first = await memories.create(
        user_id=user_id,
        payload=_rel(
            "Drawing Extractor uses Gemini",
            "Drawing Extractor",
            "uses",
            "Gemini",
            source={"source_type": "message", "reference": msgs[0].id, "location": "0:10"},
        ),
    )
    # Corroborated: a second message and a document chunk, on the same memory.
    await memories.attach_source(
        user_id=user_id,
        memory_id=first.memory.id,
        source_type=SourceType.MESSAGE,
        reference=msgs[1].id,
        location="0:5",
    )
    await memories.attach_source(
        user_id=user_id,
        memory_id=first.memory.id,
        source_type=SourceType.DOCUMENT,
        reference=document.id,
        location=f"{format_chunk_source_location(chunk.id)}:0:27",
    )
    second = await memories.create(
        user_id=user_id,
        payload=_rel(
            "DE relies on Gemini",
            "Drawing Extractor",
            "uses",
            "Gemini",
            source={"source_type": "message", "reference": str(uuid.uuid4())},  # dangling
        ),
    )
    third = await memories.create(
        user_id=user_id,
        payload=_rel("Drawing Extractor uses Redis", "Drawing Extractor", "uses", "Redis"),
    )
    retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
    ids = {
        n: (await retrieval.find_entity_by_name(user_id=user_id, name=n)).id
        for n in ("Drawing Extractor", "Gemini", "Redis")
    }
    # A legacy/seeded edge with no asserting memory at all.
    await graph.write(
        """
        MATCH (a:Entity {id: $a, user_id: $u}), (b:Entity {id: $b, user_id: $u})
        CREATE (a)-[:RELATES_TO {id: $rid, user_id: $u, predicate: 'depends_on',
                                 status: 'active', confidence: 1.0,
                                 created_at: $t, updated_at: $t}]->(b)
        """,
        {
            "a": ids["Drawing Extractor"],
            "b": ids["Redis"],
            "u": user_id,
            "rid": str(uuid.uuid4()),
            "t": JAN,
        },
    )
    clock.advance_to(JUN)
    return {
        "ids": ids,
        "first": first.memory.id,
        "second": second.memory.id,
        "third": third.memory.id,
        "msgs": [m.id for m in msgs],
        "doc": document.id,
        "chunk": chunk.id,
    }


class TestEvidence:
    async def test_multiple_memories_and_sources_in_one_response(
        self,
        db,
        graph,
        clock,
        user_id,
        evidence_world,  # noqa: ANN001
    ) -> None:
        w = evidence_world
        async with db.session() as session:
            ev = await _service(graph, clock, session=session).edge_evidence(
                user_id=user_id,
                source_entity_id=w["ids"]["Drawing Extractor"],
                target_entity_id=w["ids"]["Gemini"],
                predicate="uses",
            )
        assert ev.evidence_available is True
        assert ev.truncated is False
        by_memory = {a.memory_id: a for a in ev.assertions}
        assert set(by_memory) == {w["first"], w["second"]}

        first = by_memory[w["first"]]
        assert first.evidence_status is EvidenceStatus.AVAILABLE
        srcs = first.memory.sources  # type: ignore[union-attr]
        assert len(srcs) == 3 and all(s.resolved for s in srcs)
        assert {s.message_id for s in srcs if s.message_id} == set(w["msgs"])
        doc_src = next(s for s in srcs if s.document is not None)
        assert doc_src.document.filename == "design.md"  # type: ignore[union-attr]
        assert doc_src.document_chunk.page_start == 3  # type: ignore[union-attr]
        assert doc_src.document_chunk.heading_context == "Architecture"  # type: ignore[union-attr]

        second = by_memory[w["second"]]
        assert second.evidence_status is EvidenceStatus.SOURCES_UNRESOLVED
        assert second.memory.sources[0].resolved is False  # type: ignore[union-attr]

        kinds = {n.kind for n in ev.nodes}
        assert kinds == {NodeKind.MEMORY, NodeKind.SOURCE}
        assert len([e for e in ev.edges if e.kind is EdgeKind.DERIVED_FROM]) == 4

    async def test_memory_without_sources_is_reported_not_fabricated(
        self,
        db,
        graph,
        clock,
        user_id,
        evidence_world,  # noqa: ANN001
    ) -> None:
        w = evidence_world
        async with db.session() as session:
            ev = await _service(graph, clock, session=session).edge_evidence(
                user_id=user_id,
                source_entity_id=w["ids"]["Drawing Extractor"],
                target_entity_id=w["ids"]["Redis"],
                predicate="uses",
            )
        assert [a.evidence_status for a in ev.assertions] == [EvidenceStatus.NO_SOURCES]
        assert ev.evidence_available is False

    async def test_edge_with_no_asserting_memory_is_reported(
        self,
        db,
        graph,
        clock,
        user_id,
        evidence_world,  # noqa: ANN001
    ) -> None:
        w = evidence_world
        async with db.session() as session:
            ev = await _service(graph, clock, session=session).edge_evidence(
                user_id=user_id,
                source_entity_id=w["ids"]["Drawing Extractor"],
                target_entity_id=w["ids"]["Redis"],
                predicate="depends_on",
            )
        assert len(ev.assertions) == 1
        assert ev.assertions[0].evidence_status is EvidenceStatus.NO_ASSERTING_MEMORY
        assert ev.assertions[0].memory is None
        assert ev.evidence_available is False

    async def test_no_matching_edge_is_an_empty_answer(
        self,
        db,
        graph,
        clock,
        user_id,
        evidence_world,  # noqa: ANN001
    ) -> None:
        w = evidence_world
        async with db.session() as session:
            ev = await _service(graph, clock, session=session).edge_evidence(
                user_id=user_id,
                source_entity_id=w["ids"]["Gemini"],
                target_entity_id=w["ids"]["Drawing Extractor"],
                predicate="uses",
            )
        assert ev.assertions == [] and ev.evidence_available is False

    async def test_limit_truncates_exactly(
        self,
        db,
        graph,
        clock,
        user_id,
        evidence_world,  # noqa: ANN001
    ) -> None:
        w = evidence_world
        async with db.session() as session:
            service = _service(graph, clock, session=session)
            one = await service.edge_evidence(
                user_id=user_id,
                source_entity_id=w["ids"]["Drawing Extractor"],
                target_entity_id=w["ids"]["Gemini"],
                predicate="uses",
                limit=1,
            )
            two = await service.edge_evidence(
                user_id=user_id,
                source_entity_id=w["ids"]["Drawing Extractor"],
                target_entity_id=w["ids"]["Gemini"],
                predicate="uses",
                limit=2,
            )
        assert len(one.assertions) == 1 and one.truncated is True
        assert len(two.assertions) == 2 and two.truncated is False

    async def test_postgres_round_trips_do_not_grow_with_sources(
        self,
        db,
        graph,
        clock,
        user_id,
        evidence_world,  # noqa: ANN001
    ) -> None:
        """No N+1: one Cypher read and at most three SQL statements (messages,
        documents, chunks) however many memories/sources are resolved."""
        from sqlalchemy import event

        w = evidence_world
        statements: list[str] = []
        reads: list[str] = []
        original_read = graph.read

        async def counting_read(query, params=None):  # noqa: ANN001, ANN202
            reads.append(query)
            return await original_read(query, params)

        graph.read = counting_read  # type: ignore[method-assign]
        try:
            async with db.session() as session:
                engine = session.bind.sync_engine  # type: ignore[union-attr]

                def count(*_a: object, **_k: object) -> None:
                    statements.append("sql")

                event.listen(engine, "before_cursor_execute", count)
                try:
                    await _service(graph, clock, session=session).edge_evidence(
                        user_id=user_id,
                        source_entity_id=w["ids"]["Drawing Extractor"],
                        target_entity_id=w["ids"]["Gemini"],
                        predicate="uses",
                    )
                finally:
                    event.remove(engine, "before_cursor_execute", count)
        finally:
            graph.read = original_read  # type: ignore[method-assign]
        # 2 entity resolutions + 1 evidence read.
        assert len(reads) == 3
        assert len(statements) <= 3


# -- Cross-user isolation on every id-accepting parameter --------------------------------


class TestIsolation:
    async def test_every_endpoint_denies_another_users_ids(
        self,
        db,
        graph,
        clock,
        user_id,
        other_user_id,
        world,
        pg_cleanup,  # noqa: ANN001
    ) -> None:
        de = world["e"]["Drawing Extractor"]
        async with db.session() as session:
            service = _service(graph, clock, session=session)
            with pytest.raises(NotFoundError):
                await service.neighborhood(user_id=other_user_id, entity_id=de)
            with pytest.raises(NotFoundError):
                await service.neighborhood(
                    user_id=other_user_id, entity_id=world["e"]["Drawing Extraction Engine"]
                )
            with pytest.raises(NotFoundError):
                await service.edge_evidence(
                    user_id=other_user_id,
                    source_entity_id=de,
                    target_entity_id=world["e"]["Gemini"],
                    predicate="uses",
                )
            with pytest.raises(NotFoundError):
                await service.memory_timeline(user_id=other_user_id, memory_id=world["m"]["paddle"])
            overview = await service.overview(
                user_id=other_user_id,
                temporal_scope=TemporalScope.ALL,
                include={GraphInclude.MEMORIES, GraphInclude.MERGES},
            )
        assert overview.nodes == [] and overview.edges == []

    async def test_mixed_ownership_evidence_is_denied(
        self,
        db,
        graph,
        clock,
        user_id,
        other_user_id,
        world,
        pg_cleanup,  # noqa: ANN001
    ) -> None:
        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        await memories.create(
            user_id=other_user_id,
            payload=_rel("Mine uses Gemini", "Mine", "uses", "Gemini"),
        )
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
        mine = await retrieval.find_entity_by_name(user_id=other_user_id, name="Mine")
        async with db.session() as session:
            with pytest.raises(NotFoundError):
                await _service(graph, clock, session=session).edge_evidence(
                    user_id=other_user_id,
                    source_entity_id=mine.id,
                    target_entity_id=world["e"]["Gemini"],
                    predicate="uses",
                )

    async def test_foreign_exclude_ids_have_no_effect(
        self,
        graph,
        clock,
        user_id,
        other_user_id,
        world,  # noqa: ANN001
    ) -> None:
        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        theirs = await memories.create(
            user_id=other_user_id, payload=_rel("X uses Y", "X Thing", "uses", "Y Thing")
        )
        foreign_ids = [e.id for e in theirs.entities]
        service = _service(graph, clock)
        de = world["e"]["Drawing Extractor"]
        plain = await service.neighborhood(user_id=user_id, entity_id=de, hops=2)
        with_foreign = await service.neighborhood(
            user_id=user_id, entity_id=de, hops=2, exclude_ids=foreign_ids
        )
        assert plain.model_dump_json() == with_foreign.model_dump_json()

    async def test_traversal_never_crosses_into_another_users_subgraph(
        self,
        graph,
        clock,
        user_id,
        other_user_id,
        world,  # noqa: ANN001
    ) -> None:
        """Another user's same-named entities and edges are invisible."""
        memories = MemoryService(MemoryRepository(graph), now_fn=clock)
        await memories.create(
            user_id=other_user_id,
            payload=_rel("Drawing Extractor uses Secret", "Drawing Extractor", "uses", "Secret"),
        )
        s = await _service(graph, clock).neighborhood(
            user_id=user_id,
            entity_id=world["e"]["Drawing Extractor"],
            hops=2,
            temporal_scope=TemporalScope.ALL,
            include={GraphInclude.MEMORIES},
            max_nodes=200,
        )
        assert "Secret" not in _entity_names(s)
        assert all("Secret" not in n.label for n in s.nodes)


# -- D2: merged-away entities hidden from normal entity search --------------------------


class TestEntitySearchHidesMergedAway:
    async def test_default_search_hides_merged_away_but_ids_still_resolve(
        self,
        graph,
        clock,
        user_id,
        other_user_id,
        world,  # noqa: ANN001
    ) -> None:
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
        hits = await retrieval.search_entities(user_id=user_id, query="drawing", limit=25, offset=0)
        names = {e.name for e in hits}
        assert "Drawing Extractor" in names
        assert "Drawing Extraction Engine" not in names

        with_merged = await retrieval.search_entities(
            user_id=user_id, query="drawing", limit=25, offset=0, include_merged=True
        )
        assert "Drawing Extraction Engine" in {e.name for e in with_merged}

        detail = await retrieval.get_entity(
            user_id=user_id, entity_id=world["e"]["Drawing Extraction Engine"]
        )
        assert detail.entity.id == world["e"]["Drawing Extractor"]

        assert (
            await retrieval.search_entities(
                user_id=other_user_id, query="drawing", limit=25, offset=0, include_merged=True
            )
            == []
        )

    async def test_merged_name_is_still_findable_through_the_canonical_alias(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
        hits = await retrieval.search_entities(
            user_id=user_id, query="extraction engine", limit=25, offset=0
        )
        assert [e.name for e in hits] == ["Drawing Extractor"]


# -- D1: the existing GET /entities/{id}/graph is now capped (Stage 0-justified) ----------


class TestExistingNeighbourhoodCap:
    @pytest.mark.parametrize(
        ("max_nodes", "expected", "truncated"), [(4, 4, True), (12, 12, True), (13, 13, False)]
    )
    async def test_node_cap_is_exact(
        self,
        graph,
        clock,
        user_id,
        hub,
        max_nodes,
        expected,
        truncated,  # noqa: ANN001
    ) -> None:
        retrieval = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=clock, max_nodes=max_nodes
        )
        n = await retrieval.get_neighbourhood(user_id=user_id, entity_id=hub["hub"], hops=2)
        assert len(n.nodes) == expected
        assert n.truncated is truncated
        # Deterministic: hops, then name, then id.
        assert [x.hops for x in n.nodes] == sorted(x.hops for x in n.nodes)

    @pytest.mark.parametrize(("max_edges", "truncated"), [(5, True), (13, False)])
    async def test_edge_cap_is_exact(
        self,
        graph,
        clock,
        user_id,
        hub,
        max_edges,
        truncated,  # noqa: ANN001
    ) -> None:
        retrieval = RetrievalService(
            EntityRepository(graph), max_hops=4, now_fn=clock, max_edges=max_edges
        )
        n = await retrieval.get_neighbourhood(user_id=user_id, entity_id=hub["hub"], hops=2)
        assert len(n.relationships) == min(max_edges, 13)
        assert n.truncated is truncated

    async def test_uncapped_result_is_unchanged_below_the_cap(
        self,
        graph,
        clock,
        user_id,
        world,  # noqa: ANN001
    ) -> None:
        """Below the cap the endpoint's semantics are exactly as before:
        superseded edges are still included (it never had a temporal filter)."""
        retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)
        n = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=world["e"]["Drawing Extractor"], hops=1
        )
        assert n.truncated is False
        assert "EasyOCR" in {x.entity.name for x in n.nodes}
