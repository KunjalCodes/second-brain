"""Temporal validity and status-transition rules.

These rules decide whether "what was I using in March?" answers correctly, so they
are tested exhaustively rather than by example.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.errors import ValidationError
from app.models.enums import MemoryStatus
from app.models.lifecycle import (
    ALLOWED_TRANSITIONS,
    can_transition,
    is_currently_believed,
    is_valid_at,
    validate_window,
)
from tests.conftest import JAN, JUN, MAR, MAY


class TestValidityInterval:
    def test_instant_inside_window_is_valid(self) -> None:
        assert is_valid_at(JAN, MAY, MAR) is True

    def test_start_is_inclusive(self) -> None:
        assert is_valid_at(JAN, MAY, JAN) is True

    def test_end_is_exclusive(self) -> None:
        """The half-open convention: a fact ending in May is not true *at* May."""
        assert is_valid_at(JAN, MAY, MAY) is False

    def test_before_start_is_invalid(self) -> None:
        assert is_valid_at(MAR, MAY, JAN) is False

    def test_after_end_is_invalid(self) -> None:
        assert is_valid_at(JAN, MAY, JUN) is False

    def test_open_ended_window_is_valid_indefinitely(self) -> None:
        assert is_valid_at(JAN, None, JUN) is True

    def test_missing_start_means_always_until_end(self) -> None:
        assert is_valid_at(None, MAY, JAN) is True
        assert is_valid_at(None, MAY, JUN) is False

    def test_fully_open_window_is_always_valid(self) -> None:
        assert is_valid_at(None, None, MAR) is True

    def test_adjacent_windows_neither_overlap_nor_gap(self) -> None:
        """Two consecutive facts: exactly one is true at the switchover instant."""
        first = is_valid_at(JAN, MAY, MAY)
        second = is_valid_at(MAY, None, MAY)
        assert [first, second] == [False, True]


class TestValidateWindow:
    def test_accepts_ordered_window(self) -> None:
        validate_window(JAN, MAY)

    def test_accepts_open_ended(self) -> None:
        validate_window(JAN, None)
        validate_window(None, None)

    def test_rejects_reversed_window(self) -> None:
        with pytest.raises(ValidationError, match="strictly after"):
            validate_window(MAY, JAN)

    def test_rejects_zero_length_window(self) -> None:
        """A half-open zero-length window describes a fact that is never true."""
        with pytest.raises(ValidationError):
            validate_window(JAN, JAN)


class TestCurrentlyBelieved:
    def test_active_and_open_is_current(self) -> None:
        assert is_currently_believed(MemoryStatus.ACTIVE, None, JUN) is True

    def test_conflicting_still_counts_as_believed(self) -> None:
        """Conflicting memory is unresolved, not discarded."""
        assert is_currently_believed(MemoryStatus.CONFLICTING, None, JUN) is True

    @pytest.mark.parametrize(
        "status",
        [
            MemoryStatus.SUPERSEDED,
            MemoryStatus.HISTORICAL,
            MemoryStatus.INVALID,
            MemoryStatus.ARCHIVED,
        ],
    )
    def test_non_believed_statuses_are_not_current(self, status: MemoryStatus) -> None:
        assert is_currently_believed(status, None, JUN) is False

    def test_active_with_elapsed_window_is_not_current(self) -> None:
        """An ACTIVE memory whose window has closed must not read as current."""
        assert is_currently_believed(MemoryStatus.ACTIVE, MAY, JUN) is False


class TestStatusTransitions:
    def test_same_status_is_always_allowed(self) -> None:
        for status in MemoryStatus:
            assert can_transition(status, status) is True

    @pytest.mark.parametrize(
        "target",
        [
            MemoryStatus.HISTORICAL,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.INVALID,
            MemoryStatus.CONFLICTING,
            MemoryStatus.ARCHIVED,
        ],
    )
    def test_active_can_move_anywhere(self, target: MemoryStatus) -> None:
        assert can_transition(MemoryStatus.ACTIVE, target) is True

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (MemoryStatus.SUPERSEDED, MemoryStatus.ACTIVE),
            (MemoryStatus.HISTORICAL, MemoryStatus.ACTIVE),
            (MemoryStatus.INVALID, MemoryStatus.ACTIVE),
            (MemoryStatus.ARCHIVED, MemoryStatus.ACTIVE),
        ],
    )
    def test_closed_memory_cannot_become_current_again(
        self, current: MemoryStatus, target: MemoryStatus
    ) -> None:
        """Re-asserting a fact must create a new memory, not revive an old one."""
        assert can_transition(current, target) is False

    def test_archived_is_terminal(self) -> None:
        assert ALLOWED_TRANSITIONS[MemoryStatus.ARCHIVED] == frozenset()
        for target in MemoryStatus:
            if target is not MemoryStatus.ARCHIVED:
                assert can_transition(MemoryStatus.ARCHIVED, target) is False

    def test_every_status_has_an_explicit_rule(self) -> None:
        """A new status must be given a transition rule, not silently default."""
        assert set(ALLOWED_TRANSITIONS) == set(MemoryStatus)

    def test_transitions_never_target_an_unknown_status(self) -> None:
        for targets in ALLOWED_TRANSITIONS.values():
            assert targets <= set(MemoryStatus)


def test_timezone_aware_comparison() -> None:
    """All stored instants are timezone-aware; mixing naive values would raise."""
    aware = datetime(2026, 3, 1, tzinfo=UTC)
    assert is_valid_at(JAN, MAY, aware) is True
