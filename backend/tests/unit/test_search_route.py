"""POST /search serializes results whose explanation carries graph-path data.

Regression: `RetrievalExplanation` gained agent-internal `graph_distance` /
`graph_path` fields (Phase 5) that the `extra="forbid"` response view does not
declare; spreading the dataclass into the view made every non-empty search a
500. No earlier test exercised the route with a result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_hybrid_retrieval_service
from app.main import app
from app.models.domain import GraphPathStep
from app.schemas.memory import MemoryCreate
from app.services.hybrid_retrieval_service import HybridSearchResult, RetrievalExplanation
from app.services.memory_service import MemoryService
from tests.conftest import JAN, FrozenClock, SequentialIds
from tests.unit.fakes import FakeMemoryRepository

USER = {"X-User-Id": "search-user"}


class _FakeHybrid:
    def __init__(self, results: list[HybridSearchResult]) -> None:
        self.results = results

    async def search(self, *, user_id: str, query: str, limit: int) -> list[HybridSearchResult]:
        return self.results[:limit]


@pytest.fixture
def client_with_result() -> Iterator[TestClient]:
    service = MemoryService(FakeMemoryRepository(), now_fn=FrozenClock(JAN), id_fn=SequentialIds())  # type: ignore[arg-type]
    detail = asyncio.run(
        service.create(
            user_id="search-user",
            payload=MemoryCreate.model_validate(
                {"content": "Atlas uses PaddleOCR", "entities": [{"name": "Atlas"}]}
            ),
        )
    )
    step = GraphPathStep(entity_id="e1", entity_name="Atlas")
    result = HybridSearchResult(
        memory=detail,
        explanation=RetrievalExplanation(
            memory_id=detail.memory.id,
            retrieved_via=["graph", "keyword"],
            vector_similarity=None,
            graph_relevance=0.5,
            keyword_match=1.0,
            temporal_fit=1.0,
            confidence=0.9,
            final_score=0.8,
            temporal_state="current",
            graph_distance=1,
            graph_path=[step],
        ),
    )
    app.dependency_overrides[get_hybrid_retrieval_service] = lambda: _FakeHybrid([result])
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_search_with_graph_path_result_returns_public_explanation(
    client_with_result: TestClient,
) -> None:
    r = client_with_result.post("/search", json={"query": "OCR"}, headers=USER)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert item["memory"]["memory"]["content"] == "Atlas uses PaddleOCR"
    assert item["explanation"]["retrieved_via"] == ["graph", "keyword"]
    assert item["explanation"]["graph_relevance"] == 0.5
    # Agent-internal path data is not part of the public response contract.
    assert "graph_distance" not in item["explanation"]
    assert "graph_path" not in item["explanation"]
