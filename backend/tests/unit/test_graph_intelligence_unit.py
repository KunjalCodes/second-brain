# ruff: noqa: E501 - fixture rows are kept one per line
"""Pure helpers of the Phase 15 graph-intelligence service (no I/O)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.enums import EntityType, MemoryStatus
from app.repositories.graph_repository import AggregatedEdge
from app.schemas.graph import TemporalScope, TemporalState
from app.schemas.graph_intelligence import (
    ContextRelationship,
    EvidenceBasis,
    RelationKind,
    SupportingMemory,
)
from app.semantic.query_understanding import TemporalIntent
from app.services.graph_intelligence_service import (
    CAUSAL_PREDICATES,
    POSSIBLE_CAUSE_PREDICATES,
    _edge_basis,
    _intent_for,
    _rank_related,
    _why,
    relation_kind,
)

JAN = datetime(2026, 1, 15, tzinfo=UTC)
MAY = datetime(2026, 5, 1, tzinfo=UTC)


def _support(**kw) -> SupportingMemory:  # noqa: ANN003
    base = {
        "memory_id": "m1", "content": "x", "status": MemoryStatus.ACTIVE, "valid_from": JAN,
        "valid_until": None, "created_at": JAN, "superseded_by": [], "conflicts_with": [],
        "sources": [],
    }  # fmt: skip
    return SupportingMemory(**{**base, **kw})


def _edge(**kw) -> AggregatedEdge:  # noqa: ANN003
    base = {
        "source": "a", "target": "b", "predicate": "uses", "status": "active",
        "state": "current", "memory_ids": ["m1"], "assertion_count": 1, "confidence": 0.9,
        "valid_from": None, "valid_until": None,
    }  # fmt: skip
    return AggregatedEdge(**{**base, **kw})


class TestRelationKind:
    def test_only_stated_causal_predicates_are_causal(self) -> None:
        for p in CAUSAL_PREDICATES:
            assert relation_kind(p) is RelationKind.CAUSAL
        for p in POSSIBLE_CAUSE_PREDICATES:
            assert relation_kind(p) is RelationKind.POSSIBLE_CAUSE

    def test_sequence_and_association_predicates_are_never_causal(self) -> None:
        for p in ("followed_by", "before", "after", "preceded", "uses", "related_to", "affected"):
            assert relation_kind(p) is RelationKind.ASSOCIATION

    def test_the_sets_do_not_overlap(self) -> None:
        assert not (CAUSAL_PREDICATES & POSSIBLE_CAUSE_PREDICATES)


class TestEvidenceBasis:
    def test_a_single_current_assertion_is_directly_stated_only(self) -> None:
        assert _edge_basis(_edge(), TemporalScope.CURRENT) == [EvidenceBasis.DIRECTLY_STATED]

    def test_several_assertions_say_so(self) -> None:
        basis = _edge_basis(
            _edge(assertion_count=2, memory_ids=["m1", "m2"]), TemporalScope.CURRENT
        )
        assert EvidenceBasis.SUPPORTED_BY_MULTIPLE_MEMORIES in basis

    def test_a_no_longer_true_edge_is_temporally_derived_outside_the_current_scope(self) -> None:
        edge = _edge(state="historical", status="superseded")
        assert EvidenceBasis.TEMPORALLY_DERIVED in _edge_basis(edge, TemporalScope.ALL)
        assert EvidenceBasis.TEMPORALLY_DERIVED in _edge_basis(edge, TemporalScope.HISTORICAL)

    def test_a_current_edge_is_not_temporally_derived_even_in_the_all_scope(self) -> None:
        assert EvidenceBasis.TEMPORALLY_DERIVED not in _edge_basis(_edge(), TemporalScope.ALL)


class TestWhy:
    def test_current(self) -> None:
        text = _why(TemporalState.CURRENT, MemoryStatus.ACTIVE, JAN, None, 1, [_support()])
        assert text == "Current: stated by 1 memory, valid from 2026-01-15; no end recorded."

    def test_no_longer_true_names_the_replacement_count_and_the_end(self) -> None:
        text = _why(
            TemporalState.HISTORICAL, MemoryStatus.SUPERSEDED, JAN, MAY, 1,
            [_support(status=MemoryStatus.SUPERSEDED, superseded_by=["m9"])],
        )  # fmt: skip
        assert text == (
            "No longer true: stated by 1 memory, ended 2026-05-01 (status superseded); "
            "replaced by 1 later memory."
        )

    def test_a_dispute_is_reported(self) -> None:
        text = _why(
            TemporalState.CURRENT, MemoryStatus.CONFLICTING, JAN, None, 2,
            [_support(conflicts_with=["m7"])],
        )  # fmt: skip
        assert "stated by 2 memories" in text and "Disputed by 1 other memory" in text

    def test_not_yet_valid(self) -> None:
        assert _why(TemporalState.NOT_YET_VALID, MemoryStatus.ACTIVE, MAY, None, 1, []).startswith(
            "Not yet true"
        )

    def test_an_unrecorded_time_is_stated_as_such(self) -> None:
        text = _why(TemporalState.HISTORICAL, MemoryStatus.HISTORICAL, None, None, 1, [_support()])
        assert "an unrecorded time" in text


def _rel(other: str, predicate: str, *, current: bool, count: int = 1) -> ContextRelationship:
    return ContextRelationship(
        other_entity_id=other, other_name=other.upper(), other_entity_type=EntityType.OTHER,
        direction="outgoing", predicate=predicate, relation_kind=RelationKind.ASSOCIATION,
        status=MemoryStatus.ACTIVE if current else MemoryStatus.SUPERSEDED,
        temporal_state=TemporalState.CURRENT if current else TemporalState.HISTORICAL,
        valid_from=None, valid_until=None, assertion_count=count, confidence=0.9, memory_ids=["m"],
    )  # fmt: skip


class TestRelatedRanking:
    def test_current_and_well_supported_neighbours_rank_first(self) -> None:
        ranked = _rank_related(
            [_rel("b", "uses", current=True, count=3), _rel("c", "uses", current=True)],
            [_rel("d", "used", current=False, count=5)],
        )  # fmt: skip
        assert [r.entity_id for r in ranked] == ["b", "c", "d"]
        assert ranked[0].signals == {"current": 1.0, "support": 0.6, "relation_variety": 0.333}

    def test_ties_break_by_name_then_id_so_the_order_is_stable(self) -> None:
        a = _rank_related([_rel("y", "uses", current=True), _rel("x", "uses", current=True)], [])
        b = _rank_related([_rel("x", "uses", current=True), _rel("y", "uses", current=True)], [])
        assert [r.entity_id for r in a] == [r.entity_id for r in b] == ["x", "y"]

    def test_variety_of_relationships_raises_rank(self) -> None:
        ranked = _rank_related(
            [_rel("a", "uses", current=True), _rel("a", "depends_on", current=True), _rel("b", "uses", current=True)],
            [],
        )  # fmt: skip
        assert ranked[0].entity_id == "a"


class TestIntentFor:
    def test_an_explicit_scope_overrides_query_text_detection(self) -> None:
        assert _intent_for(None, MAY) is None  # retrieval detects it
        assert _intent_for(TemporalScope.CURRENT, MAY).temporal is TemporalIntent.CURRENT
        assert _intent_for(TemporalScope.HISTORICAL, MAY).temporal is TemporalIntent.HISTORICAL
        as_of = _intent_for(TemporalScope.AS_OF, MAY)
        assert as_of.temporal is TemporalIntent.AS_OF and as_of.as_of == MAY
        assert _intent_for(TemporalScope.ALL, MAY).temporal is TemporalIntent.UNSPECIFIED
