"""Deterministic entity-name normalization.

Phase 1 entity resolution is deliberately *only* normalization plus explicit
aliases. Semantic similarity and LLM-assisted resolution are Phase 4 (CLAUDE.md
section 13, which also warns "Do not rely entirely on the LLM").

Normalization is intentionally conservative: it lowercases and collapses runs of
whitespace, but does NOT strip internal spaces. Stripping them would make
"Open AI" and "OpenAI" collide -- which CLAUDE.md section 13 wants -- but would also
merge genuinely different names such as "Data Base" and "Database". Wrongly merging
two entities destroys user memory and is far more damaging than failing to merge, so
in Phase 1 that case is handled by an explicit alias instead of by a guess.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")

#: Shape every stored relationship predicate must satisfy. Defined once here (not
#: duplicated) because it is enforced in two places that must never drift apart:
#: `app.schemas.memory.RelationshipInput` (a human-authored API request) and
#: `app.models.extraction.CandidateRelationship` (untrusted LLM output). Rejecting
#: a malformed predicate at LLM-output-validation time, rather than discovering it
#: only when the extraction runner tries to persist it, is the whole point of
#: sharing this constant.
PREDICATE_PATTERN = r"^[a-z][a-z0-9_]{0,62}[a-z0-9]$"


def normalize_name(name: str) -> str:
    """Return the canonical lookup key for an entity name.

    Raises ValidationError for names that are empty once normalized.
    """
    from app.core.errors import ValidationError

    collapsed = _WHITESPACE.sub(" ", name).strip().lower()
    if not collapsed:
        raise ValidationError("Entity name must contain at least one non-whitespace character")
    return collapsed
