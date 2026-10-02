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
import unicodedata

from app.models.enums import EntityType
from app.models.normalization import normalize_name

#: Phase 16: entities whose identity is EXACT (a path, a commit hash, a session key).
#: `auth/session.py` and `auth_session.py` are different files; fuzzy duplicate
#: flagging is wrong for them and, because it calls an embedding provider per
#: comparison, would also spend quota on every structural entity.
EXACT_IDENTITY_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.REPOSITORY,
        EntityType.BRANCH,
        EntityType.DIRECTORY,
        EntityType.FILE,
        EntityType.CODE_SYMBOL,
        EntityType.DEPENDENCY,
        EntityType.COMMIT,
        EntityType.AGENT_SESSION,
        EntityType.TEST_RUN,
    }
)

_NON_ALPHANUMERIC = re.compile(r"[\W_]+")
#: A key this short is not evidence of anything ("C++" and "C#" both reduce to
#: "c") -- Phase 14, M3: a false merge candidate costs a reviewer's trust.
_MIN_KEY_LENGTH = 3


def canonical_key(name: str) -> str:
    """Punctuation/whitespace-insensitive form used only for ambiguity DETECTION.

    This is deliberately looser than `app.models.normalization.normalize_name`
    (which only lowercases and collapses whitespace, preserving internal spaces so
    "Data Base" is not conflated with "Database"). Using this looser key to decide
    *identity* would be unsafe for exactly that reason; using it only to flag a
    *possible* duplicate for review is the whole point of this module.
    """
    # Phase 14 (M3): Unicode-aware. The old `[^a-z0-9]` stripped every
    # non-ASCII letter, so every name written in a non-Latin script reduced to
    # "" and all of them "matched" each other. Diacritics are folded
    # ("Zoë" ~ "Zoe"); letters of every script are kept.
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALPHANUMERIC.sub("", folded)


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
    key = canonical_key(name_a)
    return len(key) >= _MIN_KEY_LENGTH and key == canonical_key(name_b)
