"""The hybrid retrieval ranking formula -- PURE, no I/O (PHASE3_PLAN.md section 8.1).

An explicit, configurable **linear weighted sum**, deliberately not a learned
reranker: a transparent formula is directly explainable per-candidate
(`RetrievalExplanation`), which a trained model is not, and nothing about this
project's scale justifies the added commitment (training data, drift, another
artifact to version) a learned reranker would bring. If evaluation ever shows the
linear formula is insufficient, that is a documented, evidence-based case for
revisiting it -- not a default assumption now.

Every weight lives in `Settings` (`RETRIEVAL_WEIGHT_*`), never hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RankingWeights:
    vector: float
    graph: float
    keyword: float
    recency: float
    temporal: float
    confidence: float


@dataclass(frozen=True)
class RankingInput:
    """Every signal already computed for one candidate memory.

    A `None` field means that retriever never found this candidate -- contributes
    0 to the score, never penalizes (PHASE3_PLAN.md section 8.2: "absence of a
    graph signal is neutral, not negative", applied uniformly to every channel).
    `recency_score` and `temporal_fit` are always present (computed once the
    candidate is known at all, regardless of which retriever(s) found it).
    """

    memory_id: str
    vector_similarity: float | None
    graph_relevance: float | None
    keyword_match: float | None
    recency_score: float
    temporal_fit: float
    confidence: float


def rank_score(candidate: RankingInput, weights: RankingWeights) -> float:
    """The formula from PHASE3_PLAN.md section 8.1, verbatim."""
    return (
        weights.vector * (candidate.vector_similarity or 0.0)
        + weights.graph * (candidate.graph_relevance or 0.0)
        + weights.keyword * (candidate.keyword_match or 0.0)
        + weights.recency * candidate.recency_score
        + weights.temporal * candidate.temporal_fit
        + weights.confidence * candidate.confidence
    )


def rank(
    candidates: list[RankingInput], weights: RankingWeights, *, top_k: int
) -> list[tuple[RankingInput, float]]:
    """Score every candidate, sort descending, keep the top `top_k`.

    Ties (identical score) keep the candidates' original relative order (Python's
    sort is stable) rather than an undefined order -- matters for deterministic
    tests and reproducible evaluation runs.
    """
    scored = [(c, rank_score(c, weights)) for c in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]
