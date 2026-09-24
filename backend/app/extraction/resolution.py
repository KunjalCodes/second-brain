"""Entity resolution extension: ambiguity detection without merging (Scenario G).

Phase 1 resolution is normalization plus explicit aliases only (see
`app/models/normalization.py`): "Open AI" and "OpenAI" are, by design, two
different entities unless an alias links them, because merging on weak similarity
risks destroying memory while missing a merge only risks a missed connection.

This module adds exactly one thing on top of that, per the approved Phase 2 scope:
it *detects* plausible duplicates using a narrow, fully deterministic signal --
punctuation- and whitespace-insensitive equality of the normalized name -- and
records the suspicion for a human (or a later, better-evidenced phase) to review.
**It never merges.** No fuzzy string distance, no semantic similarity, no LLM
judgement: exactly the "jump straight to fuzzy matching" this phase was told not to
take.
"""

from __future__ import annotations

import re

from app.models.normalization import normalize_name

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def canonical_key(name: str) -> str:
    """Punctuation/whitespace-insensitive form used only for ambiguity DETECTION.

    This is deliberately looser than `app.models.normalization.normalize_name`
    (which only lowercases and collapses whitespace, preserving internal spaces so
    "Data Base" is not conflated with "Database"). Using this looser key to decide
    *identity* would be unsafe for exactly that reason; using it only to flag a
    *possible* duplicate for review is the whole point of this module.
    """
    return _NON_ALPHANUMERIC.sub("", name.lower())


def is_plausible_duplicate(name_a: str, name_b: str) -> bool:
    """Whether two entity names are worth flagging for human review.

    True for "Open AI" / "OpenAI" (both canonicalize to "openai"). Also true for
    "Data Base" / "Database" -- that pair canonicalizes identically too, which is
    exactly why this function only ever *flags*: the caller
    (`app/extraction/decisions.py`) must never treat `True` here as license to
    merge, rename, or share identity between the two entities. A pair that Phase
    1's own `normalize_name` already treats as identical is not flagged -- there is
    nothing ambiguous to report; they already resolve to one entity.
    """
    if normalize_name(name_a) == normalize_name(name_b):
        return False
    return canonical_key(name_a) == canonical_key(name_b)
