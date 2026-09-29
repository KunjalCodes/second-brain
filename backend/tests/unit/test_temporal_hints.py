"""Deterministic temporal-hint interpretation (Phase 14, M5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.extraction.temporal_hints import interpret_temporal, parse_time_hint

SAID = datetime(2026, 6, 15, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("hint", "instant", "precision"),
    [
        ("since March 2025", datetime(2025, 3, 1, tzinfo=UTC), "month"),
        ("from Mar 2025", datetime(2025, 3, 1, tzinfo=UTC), "month"),
        ("in March of 2024", datetime(2024, 3, 1, tzinfo=UTC), "month"),
        ("since 2023-07-14", datetime(2023, 7, 14, tzinfo=UTC), "day"),
        ("since 2021", datetime(2021, 1, 1, tzinfo=UTC), "year"),
        ("until May", datetime(2026, 5, 1, tzinfo=UTC), "month"),
        # a month without a year is the most recent one, never the future
        ("since September", datetime(2025, 9, 1, tzinfo=UTC), "month"),
        ("since last year", datetime(2025, 1, 1, tzinfo=UTC), "year"),
        ("this year", datetime(2026, 1, 1, tzinfo=UTC), "year"),
        ("last month", datetime(2026, 5, 1, tzinfo=UTC), "month"),
        ("yesterday", datetime(2026, 6, 14, tzinfo=UTC), "day"),
        ("3 months ago", datetime(2026, 3, 1, tzinfo=UTC), "month"),
        ("two weeks ago", datetime(2026, 6, 1, tzinfo=UTC), "day"),
        ("since around March 2025", datetime(2025, 3, 1, tzinfo=UTC), "month"),
    ],
)
def test_supported_forms(hint: str, instant: datetime, precision: str) -> None:
    parsed = parse_time_hint(hint, reference=SAID)
    assert parsed is not None, hint
    assert (parsed.instant, parsed.precision) == (instant, precision)


@pytest.mark.parametrize(
    "hint",
    [None, "", "for a while now", "recently", "back then", "2025-13-40", "Smarch 2025", "3025"],
)
def test_unsupported_forms_are_refused_not_guessed(hint: str | None) -> None:
    assert parse_time_hint(hint, reference=SAID) is None


def test_no_hints_means_valid_from_when_stated() -> None:
    t = interpret_temporal(valid_from_hint=None, valid_until_hint=None, stated_at=SAID)
    assert (t.valid_from, t.valid_until, t.open_start, t.note) == (SAID, None, False, "")


def test_an_unparsed_hint_falls_back_and_says_so() -> None:
    t = interpret_temporal(valid_from_hint="for ages", valid_until_hint=None, stated_at=SAID)
    assert t.valid_from == SAID
    assert "not understood" in t.note


def test_a_past_fact_with_only_an_end_has_an_open_start() -> None:
    t = interpret_temporal(valid_from_hint=None, valid_until_hint="until May 2026", stated_at=SAID)
    assert t.open_start is True
    assert t.valid_from is None
    assert t.valid_until == datetime(2026, 5, 1, tzinfo=UTC)


def test_a_bounded_window_from_two_hints() -> None:
    t = interpret_temporal(
        valid_from_hint="since January 2025", valid_until_hint="until March 2026", stated_at=SAID
    )
    assert (t.valid_from, t.valid_until) == (
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=UTC),
    )


def test_contradictory_hints_never_produce_an_inverted_window() -> None:
    t = interpret_temporal(
        valid_from_hint="since March 2026", valid_until_hint="until January 2026", stated_at=SAID
    )
    assert t.valid_from == datetime(2026, 3, 1, tzinfo=UTC)
    assert t.valid_until is None
    assert "not after the start" in t.note
