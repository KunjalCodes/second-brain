"""Unit coverage for the memory graph exploration API's pure parts (Phase 8,
docs/PHASE8_PLAN.md section 19): the temporal classifier at every boundary,
budget clamping, temporal-scope resolution, and configuration hard caps. No
database -- the Cypher is exercised in tests/integration/test_graph_exploration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings
from app.core.errors import ValidationError
from app.models.lifecycle import is_valid_at
from app.repositories.graph_repository import scope_params
from app.schemas.graph import GraphBudget, GraphSlice, TemporalScope, TemporalState, temporal_state
from app.services.graph_exploration_service import clamp, relates_to_edge_id, resolve_scope

JAN = datetime(2026, 1, 15, 12, tzinfo=UTC)
MAY = datetime(2026, 5, 1, 12, tzinfo=UTC)
US = timedelta(microseconds=1)


class TestTemporalState:
    @pytest.mark.parametrize(
        ("valid_from", "valid_until", "at", "expected"),
        [
            (JAN, MAY, JAN, TemporalState.CURRENT),  # == valid_from: inclusive
            (JAN, MAY, JAN - US, TemporalState.NOT_YET_VALID),
            (JAN, MAY, MAY - US, TemporalState.CURRENT),
            (JAN, MAY, MAY, TemporalState.HISTORICAL),  # == valid_until: exclusive
            (JAN, MAY, MAY + US, TemporalState.HISTORICAL),
            (None, MAY, JAN - timedelta(days=9999), TemporalState.CURRENT),  # since forever
            (None, MAY, MAY, TemporalState.HISTORICAL),
            (JAN, None, JAN + timedelta(days=9999), TemporalState.CURRENT),  # no known end
            (None, None, JAN, TemporalState.CURRENT),
        ],
    )
    def test_half_open_boundaries(self, valid_from, valid_until, at, expected) -> None:  # noqa: ANN001
        assert temporal_state(valid_from, valid_until, at) is expected

    @pytest.mark.parametrize("offset", [JAN - US, JAN, JAN + US, MAY - US, MAY, MAY + US])
    def test_current_iff_lifecycle_says_valid(self, offset: datetime) -> None:
        """One definition of validity: `current` exactly when `is_valid_at`."""
        for window in ((JAN, MAY), (None, MAY), (JAN, None), (None, None)):
            is_current = temporal_state(*window, offset) is TemporalState.CURRENT
            assert is_current == is_valid_at(*window, offset)

    def test_instant_comparison_ignores_zone_representation(self) -> None:
        paris = timezone(timedelta(hours=1))
        same_instant = JAN.astimezone(paris)
        assert temporal_state(same_instant, None, JAN) is TemporalState.CURRENT
        assert temporal_state(None, same_instant, JAN) is TemporalState.HISTORICAL


class TestClamp:
    @pytest.mark.parametrize(
        ("requested", "cap", "expected"),
        [(None, 150, 150), (10, 150, 10), (150, 150, 150), (151, 150, 150), (10_000, 150, 150)],
    )
    def test_client_may_lower_never_raise(self, requested, cap, expected) -> None:  # noqa: ANN001
        assert clamp(requested, cap) == expected


class TestResolveScope:
    def test_current_is_now(self) -> None:
        assert resolve_scope(TemporalScope.CURRENT, None, MAY) == MAY

    def test_current_rejects_an_explicit_instant(self) -> None:
        with pytest.raises(ValidationError):
            resolve_scope(TemporalScope.CURRENT, JAN, MAY)

    def test_as_of_requires_an_instant(self) -> None:
        with pytest.raises(ValidationError):
            resolve_scope(TemporalScope.AS_OF, None, MAY)
        assert resolve_scope(TemporalScope.AS_OF, JAN, MAY) == JAN

    def test_all_defaults_to_now_and_accepts_an_instant(self) -> None:
        assert resolve_scope(TemporalScope.ALL, None, MAY) == MAY
        assert resolve_scope(TemporalScope.ALL, JAN, MAY) == JAN

    @pytest.mark.parametrize("scope", [TemporalScope.AS_OF, TemporalScope.ALL])
    def test_naive_instant_is_rejected(self, scope: TemporalScope) -> None:
        with pytest.raises(ValidationError):
            resolve_scope(scope, datetime(2026, 3, 1), MAY)  # noqa: DTZ001


class TestScopeParams:
    def test_each_scope_selects_the_documented_predicate(self) -> None:
        current = scope_params(TemporalScope.CURRENT, MAY)
        assert (current["any_status"], current["ignore_window"]) == (False, False)
        as_of = scope_params(TemporalScope.AS_OF, JAN)
        assert (as_of["any_status"], as_of["ignore_window"]) == (True, False)
        every = scope_params(TemporalScope.ALL, MAY)
        assert (every["any_status"], every["ignore_window"]) == (True, True)

    def test_excluded_and_believed_sets_match_retrieval(self) -> None:
        params = scope_params(TemporalScope.ALL, MAY)
        assert params["excluded_statuses"] == ["archived", "invalid", "merged"]
        assert params["believed_statuses"] == ["active", "conflicting"]


class TestSettingsCaps:
    def test_defaults(self) -> None:
        s = Settings()
        assert (s.graph_explore_max_nodes, s.graph_explore_max_edges) == (150, 400)
        assert (s.graph_explore_max_hops, s.graph_explore_max_memories_per_entity) == (2, 5)

    @pytest.mark.parametrize(
        ("field", "too_big"),
        [
            ("graph_explore_max_nodes", 1001),
            ("graph_explore_max_edges", 3001),
            ("graph_explore_max_hops", 4),
            ("graph_explore_max_memories_per_entity", 26),
        ],
    )
    def test_hard_caps_cannot_be_configured_away(self, field: str, too_big: int) -> None:
        with pytest.raises(PydanticValidationError):
            Settings(**{field: too_big})
        with pytest.raises(PydanticValidationError):
            Settings(**{field: 0})


class TestSliceShape:
    def test_truncation_flags_default_to_false_and_no_totals_exist(self) -> None:
        s = GraphSlice(
            temporal_scope=TemporalScope.CURRENT,
            as_of=MAY,
            budget=GraphBudget(max_nodes=1, max_edges=1),
        )
        assert (s.truncated_nodes, s.truncated_edges, s.truncated_memories) == (False,) * 3
        assert not {"total", "total_nodes", "count"} & set(s.model_dump())

    def test_aggregate_edge_id_is_deterministic_and_distinguishes_state(self) -> None:
        a = relates_to_edge_id("x", "uses", "y", "active", "current")
        assert a == relates_to_edge_id("x", "uses", "y", "active", "current")
        assert a != relates_to_edge_id("x", "uses", "y", "superseded", "historical")
