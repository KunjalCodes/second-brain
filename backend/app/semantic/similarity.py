"""Semantic similarity as evidence for the decision engine -- PURE, no I/O.

PHASE3_PLAN.md section 5/ADR 0009's central rule, restated as code:

    semantic similarity  ->  evidence  ->  decision engine

never:

    semantic similarity  ->  database mutation

Every function here takes already-computed values (a similarity score, booleans
about entity/predicate match) and returns a `RelationshipDecision` -- the exact
same type `app.extraction.decisions.classify_relationship` already returns. This
service never touches Neo4j, PostgreSQL, or an embedding client; the I/O that
produces its inputs lives in `app/extraction/runner.py`, exactly like
`app.extraction.decisions` never touches Neo4j either.

## Scope, deliberately narrow

This module is consulted ONLY when the exact-triple check
(`classify_relationship`) already found no active edge to compare against --
i.e. it produces the classification exact matching cannot: "uses Gemini for
classification" vs. "classifies with Gemini" for the SAME source entity and a
compatible predicate. It never runs against a same-predicate, different-object
case (that is the temporal-change question `classify_relationship` already owns,
and semantic similarity plays no role in replacement-vs-conflict-vs-concurrent-use
per the explicit instruction to keep that signal independent).

## Why `predicate_compatible` does not check predicate equality

This module never sees Neo4j, so it cannot compute `predicate_compatible`
itself -- the caller (`app/extraction/runner.py::_semantic_relationship_decision`)
does, and deliberately does not require an exact predicate match. By
construction, this evidence path only ever runs after exact-triple matching
(`classify_relationship`) has already found NO edge at all -- of any target --
for this candidate's exact `(source, predicate)` pair, scanning every outgoing
edge from that source entity. An existing memory asserting the *same* predicate
from the *same* source entity would therefore already have been found by that
exact check, so requiring predicate equality again here would make this branch
unreachable. What is left for semantic evidence to usefully catch is a
*differently-predicated or differently-phrased* restatement of a fact about an
entity the candidate already names (e.g. "utilizes" one turn, "uses" the next) --
gated instead on high full-statement similarity plus the matched memory actually
mentioning the relevant entity, never on a predicate string. A configurable
predicate-equivalence mapping ("uses" ~ "utilizes") would let this be more
precise but is not implemented now -- inventing a whole equivalence-class
configuration surface for a case the approved plan did not specify concrete
values for would be exactly the kind of premature complexity this project's
standing instructions warn against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.extraction.decisions import RelationshipDecision
from app.models.extraction import DecisionType


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity, for unit tests and any comparison that does
    not go through `EmbeddingRepository.search`'s SQL-side computation.

    Returns 0.0 for a zero vector rather than raising -- a degenerate embedding
    should be treated as "no similarity signal", not a crash.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


@dataclass(frozen=True)
class SemanticEvidence:
    """Everything needed to classify one existing memory as semantic evidence for
    one candidate relationship. Every field is a value the caller (the runner)
    already computed or looked up -- this dataclass carries no I/O of its own.
    """

    similarity: float
    same_source_entity: bool
    predicate_compatible: bool
    signals_change: bool
    candidate_memory_id: str


def classify_semantic_evidence(
    evidence: SemanticEvidence,
    *,
    duplicate_threshold: float,
    flag_threshold: float,
) -> RelationshipDecision:
    """`LIKELY_DUPLICATE`, `FLAGGED`, or `NEW` -- never a silent merge.

    High similarity alone is never sufficient: `same_source_entity` and
    `predicate_compatible` must both hold before similarity is even consulted,
    exactly the user's specified evidence combination (similarity + entity
    context + predicate compatibility + temporal context). `signals_change`
    demotes an otherwise-qualifying match to `FLAGGED` rather than
    `LIKELY_DUPLICATE`: a candidate that itself claims something changed is
    poor evidence that it is merely restating an existing fact, even if the
    text is semantically close -- e.g. "we now use a different Gemini setup for
    classification" is textually similar to "uses Gemini for classification" but
    is asserting a change, not a duplicate.
    """
    if not evidence.same_source_entity or not evidence.predicate_compatible:
        return RelationshipDecision(
            DecisionType.NEW,
            "no semantically similar memory shares both source entity and predicate",
        )

    if evidence.similarity >= duplicate_threshold and not evidence.signals_change:
        return RelationshipDecision(
            DecisionType.LIKELY_DUPLICATE,
            f"semantically similar ({evidence.similarity:.3f}) to an existing memory "
            f"with the same source entity and predicate; flagged for review, not merged",
            target_memory_id=evidence.candidate_memory_id,
            evidence_score=evidence.similarity,
        )

    if evidence.similarity >= flag_threshold:
        return RelationshipDecision(
            DecisionType.FLAGGED,
            f"weak semantic similarity ({evidence.similarity:.3f}) to an existing "
            f"memory with the same source entity and predicate"
            + (" and the candidate signals a change" if evidence.signals_change else ""),
            target_memory_id=evidence.candidate_memory_id,
            evidence_score=evidence.similarity,
        )

    return RelationshipDecision(
        DecisionType.NEW,
        f"similarity ({evidence.similarity:.3f}) below the flag threshold",
    )


def best_semantic_decision(
    candidates: list[SemanticEvidence],
    *,
    duplicate_threshold: float,
    flag_threshold: float,
) -> RelationshipDecision:
    """Classify every candidate existing memory and keep the strongest evidence.

    "Strongest" = highest priority (`LIKELY_DUPLICATE` over `FLAGGED` over `NEW`),
    tie-broken by similarity -- mirrors `app.extraction.decisions.overall_decision`'s
    own "worst case governs" reduction, applied here to "best evidence governs".
    """
    if not candidates:
        return RelationshipDecision(DecisionType.NEW, "no semantically similar memories found")

    priority = {DecisionType.LIKELY_DUPLICATE: 0, DecisionType.FLAGGED: 1, DecisionType.NEW: 2}
    decisions = [
        classify_semantic_evidence(
            c, duplicate_threshold=duplicate_threshold, flag_threshold=flag_threshold
        )
        for c in candidates
    ]
    paired = sorted(
        zip(decisions, candidates, strict=True),
        key=lambda pair: (priority[pair[0].decision], -pair[1].similarity),
    )
    return paired[0][0]
