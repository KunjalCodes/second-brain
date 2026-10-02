"""The deterministic memory decision engine (docs/PHASE2_PLAN.md section 4).

This is where "the LLM proposes, the application decides" (docs/adr/0007) is
actually enforced for relationship-bearing candidates. Every function here is
**pure** -- no I/O, no Neo4j, no LLM -- so the decision table itself can be tested
exhaustively against fixtures, independent of the database.

## Scope: exact-triple duplicates only

Duplicate and conflict detection compares the *canonicalized* triple
`(normalized_source, predicate, normalized_target)`. "uses Gemini for
classification" and "classifies with Gemini" are, to this engine, two unrelated
statements -- catching that requires semantic similarity, which needs embeddings.
That is explicitly **out of scope for Phase 2** (docs/PHASE2_PLAN.md, "Important
limitation") and is a stated motivation for Phase 3's hybrid/vector retrieval, not
a gap to be quietly worked around here.

## Simplification: UPDATE does not parse temporal hints

`CandidateTemporal.valid_from_hint` / `valid_until_hint` are free text ("last May")
that Phase 2 does not parse into real dates -- natural-language date parsing is its
own project and was not part of the approved scope. So "materially different
temporal data" is approximated narrowly and conservatively: a same-target
reassertion is classified `UPDATE` (bumping stored confidence) only when the
candidate's confidence is higher than what is already recorded, or when the model
itself signals `is_change=True` against an unchanged target (a contradictory but
informative signal). Otherwise a same-target reassertion is `DUPLICATE`. No
`valid_from`/`valid_until` value is ever computed by parsing hint text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models.enums import PROJECT_PREDICATES, MemoryStatus
from app.models.extraction import CandidateMemory, DecisionType
from app.models.lifecycle import is_currently_believed
from app.models.normalization import normalize_name

#: Predicates that normally hold SEVERAL values at once (Phase 14, M6). "Atlas
#: uses PostgreSQL" and "Atlas uses Redis" are concurrent facts, not a
#: contradiction -- before Phase 14 every second value was stored as
#: CONFLICTING and sent to review. Any predicate NOT listed keeps the
#: conservative single-valued behaviour (a different value without a change
#: signal is CONFLICTING), so an unknown predicate can only err towards review,
#: never towards silently accepting a contradiction.
MULTI_VALUED_PREDICATES = frozenset(
    {
        "uses", "depends_on", "works_on", "works_with", "knows", "contains", "related_to",
        "mentions", "integrates_with", "supports", "collaborates_with", "tests",
        "evaluated_against", "owns", "member_of", "part_of", "has", "maintains",
        "contributes_to", "manages", "leads", "speaks", "learned", "studies", "teaches",
        "attended", "visited", "created", "wrote", "reviewed", "plays",
    }  # fmt: skip
    # Phase 16: project history ACCUMULATES. A session that modified two files, or hit
    # two errors, has made two facts -- never a conflict.
    | PROJECT_PREDICATES
)


def is_multi_valued(predicate: str) -> bool:
    return predicate in MULTI_VALUED_PREDICATES


@dataclass(frozen=True)
class ExistingEdge:
    """One sibling edge already in the graph: same source entity and predicate."""

    target_normalized_name: str
    status: MemoryStatus
    memory_id: str
    confidence: float


@dataclass(frozen=True)
class RelationshipDecision:
    decision: DecisionType
    reason: str
    #: The existing memory this decision acts on -- the one to attach a source to
    #: (DUPLICATE), bump confidence on (UPDATE), close (SUPERSEDING), or flag
    #: for review (CONFLICTING when exactly one active conflicting fact exists,
    #: LIKELY_DUPLICATE/FLAGGED). None for NEW, IGNORE/INVALID, and CONFLICTING
    #: when more than one active fact conflicts (too ambiguous to link one).
    target_memory_id: str | None = None
    #: The similarity score behind a semantic-evidence decision (Phase 4,
    #: PHASE4_PLAN.md section 6 -- the review workflow needs the actual number,
    #: not just prose in `reason`). Set only by `app.semantic.similarity`'s
    #: LIKELY_DUPLICATE/FLAGGED branches; None everywhere else, including
    #: CONFLICTING, which is never semantic-evidence-derived.
    evidence_score: float | None = None


def classify_candidate_persistence(candidate: CandidateMemory) -> DecisionType | None:
    """`IGNORE` if the candidate should never reach the graph, else `None`.

    Applied before any relationship-level classification: a candidate the model
    itself flagged as non-persistent is not worth resolving entities for at all
    (docs/adr/0007's conservative policy -- "prefer IGNORE ... over unsafe
    persistence").
    """
    if candidate.persistence != "persistent":
        return DecisionType.IGNORE
    return None


def classify_relationship(
    *,
    predicate: str,
    target_normalized: str,
    candidate_confidence: float,
    is_change: bool,
    replaces_hint: str | None,
    existing_edges: list[ExistingEdge],
    now: datetime,
    candidate_is_past: bool = False,
) -> RelationshipDecision:
    """Classify one asserted `(source, predicate, target)` edge.

    `existing_edges` are every edge already in the graph sharing this candidate's
    source entity and predicate (any target, any non-excluded status) -- the
    caller (`app/extraction/runner.py`) is responsible for fetching exactly that
    set from Neo4j; this function does no I/O.
    """
    replaces_hint_normalized = normalize_name(replaces_hint) if replaces_hint else None

    same_target = [e for e in existing_edges if e.target_normalized_name == target_normalized]
    different_target = [e for e in existing_edges if e.target_normalized_name != target_normalized]

    active_same_target = [
        e for e in same_target if is_currently_believed(e.status, None, now)
    ]
    active_different_target = [
        e for e in different_target if is_currently_believed(e.status, None, now)
    ]

    if active_same_target:
        edge = active_same_target[0]
        if is_change:
            # A contradictory-but-informative signal: the model says something
            # changed, yet the target it named is what is already on record. Treat
            # the restatement as new information about the same fact.
            return RelationshipDecision(
                DecisionType.UPDATE,
                "candidate signals a change but names the same target already "
                "on record; treated as a restatement",
                target_memory_id=edge.memory_id,
            )
        if candidate_confidence > edge.confidence:
            return RelationshipDecision(
                DecisionType.UPDATE,
                f"restates an active fact with higher confidence "
                f"({candidate_confidence} > {edge.confidence})",
                target_memory_id=edge.memory_id,
            )
        return RelationshipDecision(
            DecisionType.DUPLICATE,
            "restates an already-active identical fact",
            target_memory_id=edge.memory_id,
        )

    if active_different_target:
        if is_change:
            hint_matches = [
                e
                for e in active_different_target
                if replaces_hint_normalized is not None
                and e.target_normalized_name == replaces_hint_normalized
            ]
            if hint_matches:
                return RelationshipDecision(
                    DecisionType.SUPERSEDING,
                    f"replaces_hint matched the existing target "
                    f"{hint_matches[0].target_normalized_name!r}",
                    target_memory_id=hint_matches[0].memory_id,
                )
            if replaces_hint_normalized is not None:
                # The model named what changed FROM, and it is not on record:
                # superseding the one fact that IS on record would attribute the
                # change to the wrong thing (Phase 14, M6).
                if is_multi_valued(predicate):
                    return RelationshipDecision(
                        DecisionType.NEW,
                        f"a change from {replaces_hint!r}, which is not a recorded "
                        f"{predicate!r} value; the recorded values are unaffected",
                    )
                return RelationshipDecision(
                    DecisionType.CONFLICTING,
                    f"a change from {replaces_hint!r} was stated, but the recorded "
                    f"value is different; flagged instead of superseding the wrong fact",
                    target_memory_id=(
                        active_different_target[0].memory_id
                        if len(active_different_target) == 1
                        else None
                    ),
                )
            if len(active_different_target) == 1:
                return RelationshipDecision(
                    DecisionType.SUPERSEDING,
                    "candidate signals a change and exactly one active fact "
                    "exists for this source and predicate",
                    target_memory_id=active_different_target[0].memory_id,
                )
            return RelationshipDecision(
                DecisionType.CONFLICTING,
                "candidate signals a change but multiple active facts exist for "
                "this source and predicate with no matching replaces_hint -- too "
                "ambiguous to safely choose which one to supersede",
            )
        if candidate_is_past:
            return RelationshipDecision(
                DecisionType.NEW,
                "describes a past state that has already ended, so it cannot "
                "contradict the current one",
            )
        if is_multi_valued(predicate):
            return RelationshipDecision(
                DecisionType.NEW,
                f"{predicate!r} can hold several values at once; the new value "
                f"coexists with the recorded one(s)",
            )
        return RelationshipDecision(
            DecisionType.CONFLICTING,
            "a different, currently active fact already exists for this source "
            "and predicate, and the candidate does not signal a change",
            # Link the two memories only when there is exactly one existing fact
            # to conflict with -- with several already-active facts (itself an
            # unusual, likely pre-existing conflict), picking one to link would be
            # an arbitrary guess, so none is linked.
            target_memory_id=(
                active_different_target[0].memory_id
                if len(active_different_target) == 1
                else None
            ),
        )

    return RelationshipDecision(
        DecisionType.NEW,
        "no currently active fact exists for this source and predicate",
    )


def overall_decision(decisions: list[RelationshipDecision]) -> RelationshipDecision:
    """Reduce a candidate's per-relationship decisions to one governing decision.

    Every documented scenario involves at most one relationship per candidate; this
    exists so a candidate asserting more than one edge still gets a single,
    deterministic classification, by priority: a SUPERSEDING or CONFLICTING
    decision on any edge governs (those are the highest-stakes outcomes and must
    not be silently outvoted by ordinary NEW/DUPLICATE edges in the same
    candidate), then UPDATE, then DUPLICATE, then NEW.
    """
    if not decisions:
        # A relationship-less candidate (a bare statement, e.g. Scenario E) has
        # nothing to classify against existing edges -- always NEW. Duplicate
        # detection for bare statements needs semantic similarity (Phase 3); see
        # the module docstring's stated limitation.
        return RelationshipDecision(DecisionType.NEW, "no relationships to classify")

    priority = {
        DecisionType.SUPERSEDING: 0,
        DecisionType.CONFLICTING: 1,
        DecisionType.UPDATE: 2,
        DecisionType.DUPLICATE: 3,
        # Phase 3 semantic-evidence outcomes rank between an exact DUPLICATE and
        # an ordinary NEW: weaker than proven-identical, but not nothing.
        DecisionType.LIKELY_DUPLICATE: 4,
        DecisionType.FLAGGED: 5,
        DecisionType.NEW: 6,
    }
    return min(decisions, key=lambda d: priority[d.decision])
