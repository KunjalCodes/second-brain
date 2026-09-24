"""Request/response schemas for hybrid search (ADR 0009, PHASE3_PLAN.md)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain import MemoryDetail


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    limit: int | None = Field(default=None, ge=1, le=200)


class RetrievalExplanationView(BaseModel):
    """Mirrors `app.services.hybrid_retrieval_service.RetrievalExplanation`
    exactly -- a `None` field means that channel never found this result, not
    that it scored zero (PHASE3_PLAN.md section 11: report computation, not a
    fabricated narrative).
    """

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    retrieved_via: list[str]
    vector_similarity: float | None
    graph_relevance: float | None
    keyword_match: float | None
    temporal_fit: float
    confidence: float
    final_score: float
    temporal_state: str


class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: MemoryDetail
    explanation: RetrievalExplanationView


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SearchResultItem]
