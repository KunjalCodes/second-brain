"""`app.semantic.query_understanding.detect_temporal_intent` -- pure, no LLM,
mirroring `test_extraction_gate.py`'s exhaustive-boundary shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.semantic.query_understanding import TemporalIntent, detect_temporal_intent

NOW = datetime(2026, 6, 15, tzinfo=UTC)


class TestCurrentIntent:
    def test_now(self) -> None:
        assert (
            detect_temporal_intent("What am I using now?", now=NOW).temporal
            is TemporalIntent.CURRENT
        )

    def test_current(self) -> None:
        assert (
            detect_temporal_intent("What is my current OCR library?", now=NOW).temporal
            is TemporalIntent.CURRENT
        )

    def test_currently(self) -> None:
        assert (
            detect_temporal_intent("What am I currently working on?", now=NOW).temporal
            is TemporalIntent.CURRENT
        )


class TestHistoricalIntent:
    def test_before(self) -> None:
        assert (
            detect_temporal_intent("What did I use before?", now=NOW).temporal
            is TemporalIntent.HISTORICAL
        )

    def test_previously(self) -> None:
        assert (
            detect_temporal_intent("What was I previously using?", now=NOW).temporal
            is TemporalIntent.HISTORICAL
        )

    def test_used_to(self) -> None:
        assert (
            detect_temporal_intent("What library did I used to use?", now=NOW).temporal
            is TemporalIntent.HISTORICAL
        )

    def test_historical_takes_priority_over_an_incidental_current_word(self) -> None:
        """A query naming the past should not be diluted by an unrelated 'now'
        elsewhere in the sentence.
        """
        result = detect_temporal_intent("Compared to now, what did I use previously?", now=NOW)
        assert result.temporal is TemporalIntent.HISTORICAL


class TestAsOfIntent:
    def test_month_only_uses_the_reference_year(self) -> None:
        result = detect_temporal_intent("What was I using in March?", now=NOW)
        assert result.temporal is TemporalIntent.AS_OF
        assert result.as_of == datetime(2026, 3, 1, tzinfo=UTC)

    def test_month_and_explicit_year(self) -> None:
        result = detect_temporal_intent("What was I using in March 2025?", now=NOW)
        assert result.temporal is TemporalIntent.AS_OF
        assert result.as_of == datetime(2025, 3, 1, tzinfo=UTC)

    def test_as_of_takes_priority_over_historical_and_current_words(self) -> None:
        result = detect_temporal_intent(
            "What did I previously use in March, and what do I use now?", now=NOW
        )
        assert result.temporal is TemporalIntent.AS_OF

    def test_case_insensitive_month_name(self) -> None:
        result = detect_temporal_intent("what was i using in MARCH?", now=NOW)
        assert result.temporal is TemporalIntent.AS_OF

    def test_unparsed_date_reference_falls_through_to_unspecified(self) -> None:
        """Deliberately narrow: 'last quarter' is not parsed rather than guessed."""
        result = detect_temporal_intent("What was I using last quarter?", now=NOW)
        assert result.temporal is TemporalIntent.UNSPECIFIED


class TestUnspecifiedIntent:
    def test_a_plain_factual_question_has_no_detected_temporal_intent(self) -> None:
        result = detect_temporal_intent("What technologies does the project use?", now=NOW)
        assert result.temporal is TemporalIntent.UNSPECIFIED
        assert result.as_of is None

    def test_defaults_do_not_crash_without_an_explicit_now(self) -> None:
        result = detect_temporal_intent("What technologies does the project use?")
        assert result.temporal is TemporalIntent.UNSPECIFIED
