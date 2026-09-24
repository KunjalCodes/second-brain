"""`app.semantic.similarity` -- pure, no I/O, exhaustively boundary-tested,
mirroring `test_extraction_decisions.py`'s shape.
"""

from __future__ import annotations

from app.extraction.decisions import RelationshipDecision
from app.models.extraction import DecisionType
from app.semantic.similarity import (
    SemanticEvidence,
    best_semantic_decision,
    classify_semantic_evidence,
    cosine_similarity,
)

_THRESHOLDS = {"duplicate_threshold": 0.90, "flag_threshold": 0.80}


def evidence(
    *,
    similarity: float,
    same_source_entity: bool = True,
    predicate_compatible: bool = True,
    signals_change: bool = False,
    candidate_memory_id: str = "mem-1",
) -> SemanticEvidence:
    return SemanticEvidence(
        similarity=similarity,
        same_source_entity=same_source_entity,
        predicate_compatible=predicate_compatible,
        signals_change=signals_change,
        candidate_memory_id=candidate_memory_id,
    )


class TestCosineSimilarity:
    def test_identical_vectors_are_similarity_one(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == 1.0

    def test_orthogonal_vectors_are_similarity_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_opposite_vectors_are_similarity_negative_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0

    def test_zero_vector_returns_zero_rather_than_dividing_by_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


class TestClassifySemanticEvidence:
    def test_high_similarity_with_matching_context_is_likely_duplicate(self) -> None:
        result = classify_semantic_evidence(evidence(similarity=0.95), **_THRESHOLDS)
        assert result.decision is DecisionType.LIKELY_DUPLICATE
        assert result.target_memory_id == "mem-1"

    def test_weak_similarity_with_matching_context_is_flagged(self) -> None:
        result = classify_semantic_evidence(evidence(similarity=0.85), **_THRESHOLDS)
        assert result.decision is DecisionType.FLAGGED
        assert result.target_memory_id == "mem-1"

    def test_similarity_below_flag_threshold_is_new(self) -> None:
        result = classify_semantic_evidence(evidence(similarity=0.5), **_THRESHOLDS)
        assert result.decision is DecisionType.NEW
        assert result.target_memory_id is None

    def test_exact_boundary_at_duplicate_threshold_is_likely_duplicate(self) -> None:
        result = classify_semantic_evidence(evidence(similarity=0.90), **_THRESHOLDS)
        assert result.decision is DecisionType.LIKELY_DUPLICATE

    def test_exact_boundary_at_flag_threshold_is_flagged(self) -> None:
        result = classify_semantic_evidence(evidence(similarity=0.80), **_THRESHOLDS)
        assert result.decision is DecisionType.FLAGGED

    def test_just_below_flag_threshold_is_new(self) -> None:
        result = classify_semantic_evidence(evidence(similarity=0.7999), **_THRESHOLDS)
        assert result.decision is DecisionType.NEW

    def test_no_shared_source_entity_is_always_new_regardless_of_similarity(self) -> None:
        result = classify_semantic_evidence(
            evidence(similarity=0.99, same_source_entity=False), **_THRESHOLDS
        )
        assert result.decision is DecisionType.NEW

    def test_no_predicate_compatibility_is_always_new_regardless_of_similarity(self) -> None:
        result = classify_semantic_evidence(
            evidence(similarity=0.99, predicate_compatible=False), **_THRESHOLDS
        )
        assert result.decision is DecisionType.NEW

    def test_high_similarity_but_signals_change_is_demoted_to_flagged_not_duplicate(self) -> None:
        """A candidate that itself claims something changed is poor evidence that
        it merely restates an existing fact, even if the text is close.
        """
        result = classify_semantic_evidence(
            evidence(similarity=0.99, signals_change=True), **_THRESHOLDS
        )
        assert result.decision is DecisionType.FLAGGED

    def test_never_returns_duplicate_or_update_or_conflicting(self) -> None:
        """Semantic evidence must never produce the exact-triple-only outcomes --
        it is a strictly separate, weaker-confidence set (ADR 0009).
        """
        for similarity in (0.0, 0.5, 0.8, 0.9, 0.99, 1.0):
            result = classify_semantic_evidence(evidence(similarity=similarity), **_THRESHOLDS)
            assert result.decision not in (
                DecisionType.DUPLICATE,
                DecisionType.UPDATE,
                DecisionType.CONFLICTING,
                DecisionType.SUPERSEDING,
            )


class TestBestSemanticDecision:
    def test_empty_candidate_list_is_new(self) -> None:
        result = best_semantic_decision([], **_THRESHOLDS)
        assert result.decision is DecisionType.NEW

    def test_picks_the_highest_priority_outcome_among_several_candidates(self) -> None:
        candidates = [
            evidence(similarity=0.85, candidate_memory_id="weak"),
            evidence(similarity=0.95, candidate_memory_id="strong"),
        ]
        result = best_semantic_decision(candidates, **_THRESHOLDS)
        assert result.decision is DecisionType.LIKELY_DUPLICATE
        assert result.target_memory_id == "strong"

    def test_ties_broken_by_higher_similarity(self) -> None:
        candidates = [
            evidence(similarity=0.99, candidate_memory_id="a"),
            evidence(similarity=0.999, candidate_memory_id="b"),
        ]
        result = best_semantic_decision(candidates, **_THRESHOLDS)
        assert result.target_memory_id == "b"

    def test_all_weak_evidence_falls_back_to_new(self) -> None:
        candidates = [evidence(similarity=0.1), evidence(similarity=0.2)]
        result = best_semantic_decision(candidates, **_THRESHOLDS)
        assert result.decision is DecisionType.NEW


def test_relationship_decision_is_reused_unmodified() -> None:
    """`classify_semantic_evidence` returns the exact same type
    `app.extraction.decisions.classify_relationship` does -- the runner treats
    both uniformly (ADR 0009: semantic evidence augments the existing decision
    engine, it does not introduce a parallel one).
    """
    result = classify_semantic_evidence(evidence(similarity=0.95), **_THRESHOLDS)
    assert isinstance(result, RelationshipDecision)
