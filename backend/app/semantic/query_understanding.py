"""Deterministic query-intent detection -- no LLM (PHASE3_PLAN.md section 9).

Mirrors `app.extraction.gate`'s own shape: a cheap, pure, exhaustively-tested
pattern match that runs before anything expensive (here, a vector search and a
Neo4j read) rather than a model call. `UNSPECIFIED` is the safe default and is
expected to be the common case -- this heuristic is deliberately narrow and will
under-detect intent for many real queries; that is an accepted, measured
limitation (evaluated, not hidden), not a claim of natural-language understanding.

An LLM-based query decomposition layer is a named, explicit, NOT-built-here future
option (PHASE3_PLAN.md section 9) -- retrieval must work correctly end to end with
zero LLM involvement before one is ever added on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class TemporalIntent(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    AS_OF = "as_of"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True)
class QueryIntent:
    temporal: TemporalIntent
    as_of: datetime | None = None  # set only when temporal is AS_OF


#: Whole-word matches only (`\b`), case-insensitive -- "current" must not match
#: inside an unrelated longer word, and matching is deliberately narrow rather
#: than an attempt at general sentiment/tense parsing.
_CURRENT_PATTERN = re.compile(r"\b(now|current|currently|today|at the moment)\b", re.IGNORECASE)
_HISTORICAL_PATTERN = re.compile(
    r"\b(before|previously|used to|earlier|in the past|historical(ly)?|formerly)\b",
    re.IGNORECASE,
)

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
#: "in March", "in March 2026" -- narrow, explicit month (+ optional year) parsing.
#: NOT general natural-language date parsing (the same scoping call already made
#: for CandidateTemporal hints in docs/PHASE2_PLAN.md) -- an unparsed date
#: reference falls through to UNSPECIFIED rather than a wrong guess.
_MONTH_PATTERN = re.compile(
    r"\bin\s+(" + "|".join(_MONTH_NAMES) + r")\b(?:\s+(\d{4}))?", re.IGNORECASE
)


def detect_temporal_intent(query: str, *, now: datetime | None = None) -> QueryIntent:
    """Classify a search query's temporal intent from surface text alone.

    Checked in a fixed priority order: an explicit month reference (AS_OF) is
    the strongest, most specific signal and is checked first; `HISTORICAL` next
    (a query naming the past should not be diluted by an incidental "now"
    elsewhere in the sentence); `CURRENT` next; otherwise `UNSPECIFIED`, which
    applies no temporal filter or ranking boost at all -- absence of detected
    intent is neutral, never a penalty.
    """
    reference_year = (now or datetime.now(UTC)).year

    month_match = _MONTH_PATTERN.search(query)
    if month_match:
        month_index = _MONTH_NAMES.index(month_match.group(1).lower()) + 1
        year = int(month_match.group(2)) if month_match.group(2) else reference_year
        return QueryIntent(TemporalIntent.AS_OF, as_of=datetime(year, month_index, 1, tzinfo=UTC))

    if _HISTORICAL_PATTERN.search(query):
        return QueryIntent(TemporalIntent.HISTORICAL)

    if _CURRENT_PATTERN.search(query):
        return QueryIntent(TemporalIntent.CURRENT)

    return QueryIntent(TemporalIntent.UNSPECIFIED)
