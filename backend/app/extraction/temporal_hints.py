"""Deterministic interpretation of extraction temporal hints (Phase 14, M5).

The extraction model reports *when* a fact held as free text
(`CandidateTemporal.valid_from_hint` / `valid_until_hint`, e.g. "since March
2025", "until May"). Until Phase 14 those strings were never read: every fact
became valid from the moment the job happened to run. This module turns the
unambiguous forms into instants, relative to when the fact was STATED (the
message's own time), and refuses everything else -- an unparsed hint falls back
to the stated time and says so; it is never guessed.

Supported (case-insensitive, after a leading "since / from / starting / as of /
in / on / until / till / through / up to / by" is stripped):

* ISO dates: `2025-03-14`
* `March 2025`, `Mar 2025`, `March of 2025`
* `March` -- the most recent March not after the stated time (never the future)
* `2025` -- a bare four-digit year (1900-2100)
* `last year` / `this year`, `last month` / `this month`, `last week`
* `N days|weeks|months|years ago`
* `today`, `yesterday`

The result carries its precision (day / month / year) so an explanation can say
"from March 2025 (month precision)" rather than pretend to know the day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],  # fmt: skip
        start=1,
    )
    for name in names
}
_LEADING = re.compile(
    r"^(?:since|from|starting(?: in| from)?|beginning(?: in)?|as of|in|on|until|till|til|"
    r"through|thru|up to|up until|by|around|about|approximately|circa)\s+",
    re.IGNORECASE,
)
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_YEAR = re.compile(r"^([a-z]+)\.?(?:\s+of)?,?\s+(\d{4})$", re.IGNORECASE)
_YEAR = re.compile(r"^(\d{4})$")
_AGO = re.compile(r"^(\d{1,3}|a|an|one|two|three)\s+(day|week|month|year)s?\s+ago$", re.IGNORECASE)
_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


@dataclass(frozen=True)
class ParsedTime:
    instant: datetime
    precision: str  # "day" | "month" | "year"

    def describe(self) -> str:
        fmt = {"day": "%Y-%m-%d", "month": "%B %Y", "year": "%Y"}[self.precision]
        return f"{self.instant.strftime(fmt)} ({self.precision} precision)"


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def _shift_months(ref: datetime, months: int) -> datetime:
    total = ref.year * 12 + (ref.month - 1) - months
    return _month_start(total // 12, total % 12 + 1)


def parse_time_hint(hint: str | None, *, reference: datetime) -> ParsedTime | None:
    """The instant a hint names, relative to `reference` (when it was said)."""
    if not hint:
        return None
    ref = reference.astimezone(UTC)
    text = hint.strip().rstrip(".").strip()
    for _ in range(2):  # "since around March" -> two leading words
        text = _LEADING.sub("", text).strip()
    lower = text.lower()

    if m := _ISO.match(lower):
        try:
            return ParsedTime(datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=UTC), "day")
        except ValueError:
            return None
    if m := _MONTH_YEAR.match(lower):
        month = _MONTHS.get(m[1])
        year = int(m[2])
        if month and 1900 <= year <= 2100:
            return ParsedTime(_month_start(year, month), "month")
        return None
    if lower in _MONTHS:
        month = _MONTHS[lower]
        year = ref.year if month <= ref.month else ref.year - 1
        return ParsedTime(_month_start(year, month), "month")
    if m := _YEAR.match(lower):
        year = int(m[1])
        if not 1900 <= year <= 2100:
            return None
        return ParsedTime(datetime(year, 1, 1, tzinfo=UTC), "year")
    day = datetime(ref.year, ref.month, ref.day, tzinfo=UTC)
    fixed = {
        "last year": ParsedTime(datetime(ref.year - 1, 1, 1, tzinfo=UTC), "year"),
        "this year": ParsedTime(datetime(ref.year, 1, 1, tzinfo=UTC), "year"),
        "last month": ParsedTime(_shift_months(ref, 1), "month"),
        "this month": ParsedTime(_month_start(ref.year, ref.month), "month"),
        "last week": ParsedTime(day - timedelta(days=7), "day"),
        "today": ParsedTime(day, "day"),
        "yesterday": ParsedTime(day - timedelta(days=1), "day"),
    }
    if lower in fixed:
        return fixed[lower]
    if m := _AGO.match(lower):
        n = _WORD_NUMBERS.get(m[1].lower()) or int(m[1])
        unit = m[2].lower()
        if unit == "day":
            return ParsedTime(day - timedelta(days=n), "day")
        if unit == "week":
            return ParsedTime(day - timedelta(weeks=n), "day")
        if unit == "month":
            return ParsedTime(_shift_months(ref, n), "month")
        return ParsedTime(datetime(ref.year - n, 1, 1, tzinfo=UTC), "year")
    return None


@dataclass(frozen=True)
class TemporalInterpretation:
    """The validity window an extracted candidate is stored with."""

    valid_from: datetime | None
    valid_until: datetime | None
    #: No known start (a past fact with only an end: "until May").
    open_start: bool
    #: Human-readable account, appended to the candidate's decision reason.
    note: str


def interpret_temporal(
    *, valid_from_hint: str | None, valid_until_hint: str | None, stated_at: datetime
) -> TemporalInterpretation:
    """Valid time = what the hints say, else when the fact was stated.

    Never produces an inverted window: an end at or before the start is
    dropped when the start came from a hint (the hints disagree), and turns the
    start into "unknown" when the start was only the stated time ("Atlas used
    Heroku until May", said in June).
    """
    start = parse_time_hint(valid_from_hint, reference=stated_at)
    end = parse_time_hint(valid_until_hint, reference=stated_at)
    notes = []
    if valid_from_hint:
        notes.append(
            f"valid from {start.describe()} per hint {valid_from_hint!r}"
            if start
            else f"hint {valid_from_hint!r} not understood; valid from when it was stated"
        )
    if valid_until_hint and not end:
        notes.append(f"end hint {valid_until_hint!r} not understood; ignored")
    valid_from = start.instant if start else stated_at
    open_start = False
    valid_until = None
    if end:
        if end.instant > valid_from:
            valid_until = end.instant
            notes.append(f"valid until {end.describe()} per hint {valid_until_hint!r}")
        elif start:
            notes.append(f"end hint {valid_until_hint!r} is not after the start; ignored")
        else:
            valid_from, open_start, valid_until = None, True, end.instant
            notes.append(
                f"valid until {end.describe()} per hint {valid_until_hint!r}; start unknown"
            )
    return TemporalInterpretation(valid_from, valid_until, open_start, "; ".join(notes))
