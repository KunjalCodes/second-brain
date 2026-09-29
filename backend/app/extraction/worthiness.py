"""Deterministic memory-worthiness policy (Phase 14, M1).

Before Phase 14 the only "should this be remembered?" check was the LLM's own
`persistence` label (`decisions.classify_candidate_persistence`). This module
puts a small, explainable, pure rule set in front of that label, so a stored
or ignored candidate can always answer *why* with a stable rule code, which the
runner records in `extraction_candidates.decision_reason` as
`[worthiness:<code>] ...`.

Order matters and is deliberately conservative:

1. **Temporary-context rules win first** (hypotheticals, arithmetic, a
   first-person activity in progress): even a "persistent" label cannot store
   "Right now I'm debugging the login page".
2. **An explicit user instruction** ("remember that...", "from now on...",
   "I prefer...") is stored even when the model hedged with "temporary" or
   "uncertain" -- the user asked for it to persist.
3. Otherwise the model's label decides, exactly as before.

The rules read the TARGET text (what the user actually said), not the model's
paraphrase, so the model cannot talk its way past them. They are narrow on
purpose: an unmatched message falls through to the previous behaviour, never
to a new guess. Being wrong in the IGNORE direction loses a memory the sweep
can never recover, so the IGNORE patterns only fire on unambiguous surface
forms; "Atlas currently runs on Kubernetes" (a current *state*) is not
transient -- only an *activity* ("currently debugging") is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.extraction import CandidateMemory

_HYPOTHETICAL = re.compile(
    r"^\s*(if|suppose|supposing|imagine|assuming|what if)\b"
    r"|\bhypothetically\b|\bif we (ever|were to)\b",
    re.IGNORECASE,
)
_ARITHMETIC = re.compile(
    r"\d+(?:\.\d+)?\s*(?:[+\-*/×x]|times|plus|minus|divided by|multiplied by)\s*\d+(?:\.\d+)?"
    r"\s*(?:=|\b(?:is|equals|gives)\b)",
    re.IGNORECASE,
)
_ACTIVITY_VERBS = (
    r"debugging|trying|testing|fixing|looking at|investigating|stuck on|reading|"
    r"writing up|poking at|troubleshooting|figuring out"
)
_TRANSIENT_ACTIVITY = re.compile(
    rf"\b(?:right now|at the moment|just now|currently)\b[^.!?]*\b(?:{_ACTIVITY_VERBS})\b"
    rf"|\bi'?m (?:currently |still |just )?(?:{_ACTIVITY_VERBS})\b"
    rf"|\bi am (?:currently |still |just )?(?:{_ACTIVITY_VERBS})\b",
    re.IGNORECASE,
)
#: A recurring activity is a durable fact ("I'm testing it every Friday").
_RECURRING = re.compile(
    r"\b(?:every|each|usually|always|weekly|daily|monthly|on (?:mondays|tuesdays|wednesdays|"
    r"thursdays|fridays|saturdays|sundays|weekends))\b",
    re.IGNORECASE,
)
_EXPLICIT_INSTRUCTION = re.compile(
    r"\b(?:please )?remember (?:that|this)\b|\bdon'?t forget\b|\bfrom now on\b|\bgoing forward\b"
    r"|\bi (?:always|never) (?:use|deploy|want|write|run|prefer|work)\b"
    r"|\bi(?:'d| would)? prefer\b|\bmy preference is\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Worthiness:
    store: bool
    #: Stable machine-readable rule code (tests, explanation UI).
    code: str
    reason: str

    def tag(self) -> str:
        return f"[worthiness:{self.code}]"


def evaluate_worthiness(candidate: CandidateMemory, *, target_text: str) -> Worthiness:
    """Decide whether a candidate is long-term memory, with a reason."""
    if _HYPOTHETICAL.search(target_text):
        return Worthiness(False, "hypothetical", "the message is hypothetical or conditional")
    if _ARITHMETIC.search(target_text):
        return Worthiness(False, "calculation", "the message is an intermediate calculation")
    if _TRANSIENT_ACTIVITY.search(target_text) and not _RECURRING.search(target_text):
        return Worthiness(
            False, "transient_activity", "the message describes an activity in progress"
        )
    if _EXPLICIT_INSTRUCTION.search(target_text):
        if candidate.persistence == "persistent":
            return Worthiness(
                True, "explicit_user_instruction", "the user asked for this to persist"
            )
        return Worthiness(
            True,
            "explicit_user_instruction",
            f"the user asked for this to persist (model label was {candidate.persistence!r})",
        )
    if candidate.persistence == "persistent":
        return Worthiness(True, "model_persistent", "the model classified it as a durable fact")
    return Worthiness(
        False,
        f"model_{candidate.persistence}",
        f"the model classified it as {candidate.persistence!r} and no rule overrides that",
    )
