"""`app.semantic.ranking` -- pure, no I/O."""

from __future__ import annotations

from app.semantic.ranking import RankingInput, RankingWeights, rank, rank_score

WEIGHTS = RankingWeights(
    vector=1.0, graph=1.0, keyword=0.5, recency=0.2, temporal=0.5, confidence=0.3
)


def make(
    *,
    memory_id: str = "m1",
    vector: float | None = None,
    graph: float | None = None,
    keyword: float | None = None,
    recency: float = 0.0,
    temporal: float = 0.0,
    confidence: float = 0.0,
) -> RankingInput:
    return RankingInput(
        memory_id=memory_id,
        vector_similarity=vector,
        graph_relevance=graph,
        keyword_match=keyword,
        recency_score=recency,
        temporal_fit=temporal,
        confidence=confidence,
    )


class TestRankScore:
    def test_all_zero_signals_score_zero(self) -> None:
        assert rank_score(make(), WEIGHTS) == 0.0

    def test_a_missing_channel_contributes_zero_not_a_penalty(self) -> None:
        """None (never found by that retriever) must score identically to an
        explicit 0.0 -- absence is neutral (PHASE3_PLAN.md section 8.2).
        """
        with_none = make(vector=None, graph=0.5)
        with_zero = make(vector=0.0, graph=0.5)
        assert rank_score(with_none, WEIGHTS) == rank_score(with_zero, WEIGHTS)

    def test_formula_is_a_weighted_linear_sum(self) -> None:
        candidate = make(
            vector=0.8, graph=0.6, keyword=1.0, recency=0.5, temporal=1.0, confidence=0.9
        )
        expected = 1.0 * 0.8 + 1.0 * 0.6 + 0.5 * 1.0 + 0.2 * 0.5 + 0.5 * 1.0 + 0.3 * 0.9
        assert rank_score(candidate, WEIGHTS) == expected

    def test_zeroing_a_weight_removes_that_signals_contribution(self) -> None:
        """The exact mechanism the Phase 3 ablation studies rely on
        (PHASE3_PLAN.md section 17): zero one weight, confirm its signal no
        longer moves the score.
        """
        no_vector_weight = RankingWeights(
            vector=0.0, graph=1.0, keyword=0.0, recency=0.0, temporal=0.0, confidence=0.0
        )
        low_vector = make(vector=0.1, graph=0.5)
        high_vector = make(vector=0.99, graph=0.5)
        assert rank_score(low_vector, no_vector_weight) == rank_score(high_vector, no_vector_weight)

    def test_higher_confidence_alone_increases_score(self) -> None:
        low = make(confidence=0.1)
        high = make(confidence=0.9)
        assert rank_score(high, WEIGHTS) > rank_score(low, WEIGHTS)


class TestRank:
    def test_sorts_descending_by_score(self) -> None:
        candidates = [make(memory_id="low", confidence=0.1), make(memory_id="high", confidence=0.9)]
        result = rank(candidates, WEIGHTS, top_k=10)
        assert [c.memory_id for c, _ in result] == ["high", "low"]

    def test_respects_top_k(self) -> None:
        candidates = [make(memory_id=str(i), confidence=i / 10) for i in range(10)]
        result = rank(candidates, WEIGHTS, top_k=3)
        assert len(result) == 3

    def test_top_k_larger_than_candidate_count_returns_everything(self) -> None:
        candidates = [make(memory_id="a"), make(memory_id="b")]
        result = rank(candidates, WEIGHTS, top_k=50)
        assert len(result) == 2

    def test_empty_candidates_returns_empty(self) -> None:
        assert rank([], WEIGHTS, top_k=10) == []

    def test_ties_keep_stable_original_order(self) -> None:
        candidates = [make(memory_id="first"), make(memory_id="second")]
        result = rank(candidates, WEIGHTS, top_k=10)
        assert [c.memory_id for c, _ in result] == ["first", "second"]
