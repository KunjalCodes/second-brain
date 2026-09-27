"""Phase 11 (M8): populated-response contract tests for routes that had none.

The Phase 10 `POST /search` 500 only appeared when a result existed: every
test had exercised the empty path. Each test here seeds REAL data, calls the
route over HTTP, and validates the body against the route's own response
model (so a model/serialization mismatch fails), plus a cross-user check.

Routes covered: all 12 `/review/*` routes, `/extraction/jobs`,
`/extraction/jobs/{id}`, and the internal sweep / backfill / reconcile.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.repositories.entity_merge_repository import EntityMergeCandidateRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.repositories.user_repository import UserRepository
from app.schemas.embedding import EmbeddingBackfillResult
from app.schemas.extraction import ExtractionJobDetail, ExtractionJobListResponse, SweepResult
from app.schemas.memory import MemoryCreate
from app.schemas.review import (
    EntityMergeCandidateDetail,
    EntityMergeCandidateListResponse,
    MemoryReviewCandidateDetail,
    MemoryReviewCandidateListResponse,
    ReviewAuditLogResponse,
)
from app.services.memory_service import MemoryService
from tests.integration.test_job_reliability import _candidate, _message_job, _settings, _tick

pytestmark = pytest.mark.integration

TOKEN = "t" * 40


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, db, graph) -> Iterator[TestClient]:  # noqa: ANN001
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c


def _h(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


async def _memory(graph, user_id: str, content: str, subject: str, obj: str):  # noqa: ANN001, ANN202
    return await MemoryService(MemoryRepository(graph)).create(
        user_id=user_id,
        payload=MemoryCreate.model_validate(
            {
                "content": content,
                "entities": [
                    {"name": subject, "entity_type": "project"},
                    {"name": obj, "entity_type": "technology"},
                ],
                "relationships": [{"source": subject, "predicate": "hosted_on", "target": obj}],
            }
        ),
    )


async def _seed_review(db, graph, user_id: str) -> dict[str, str]:  # noqa: ANN001
    async with db.session() as session:
        await UserRepository(session).ensure_exists(user_id, now=datetime.now(UTC))
    a = await _memory(graph, user_id, "Atlas is hosted on Fly.io.", "Atlas", "Fly.io")
    b = await _memory(graph, user_id, "Atlas is hosted on Render.", "Atlas", "Render")
    c = await _memory(graph, user_id, "Atlas uses OpenAI embeddings.", "Atlas", "OpenAI")
    d = await _memory(graph, user_id, "Atlas was evaluated on Open AI models.", "Atlas", "Open AI")
    e = await _memory(graph, user_id, "Atlas runs on Kubernetes clusters.", "Atlas", "Kubernetes")
    f = await _memory(graph, user_id, "Atlas is deployed on Kubernetes.", "Atlas", "K8s")
    ids = {
        "same": str(uuid.uuid4()),
        "conflict": str(uuid.uuid4()),
        "duplicate": str(uuid.uuid4()),
        "merge": str(uuid.uuid4()),
        "merge2": str(uuid.uuid4()),
        "mem_a": a.memory.id,
        "mem_b": b.memory.id,
    }
    ent = {e.name: e.id for detail in (c, d) for e in detail.entities}
    now = datetime.now(UTC)
    async with db.session() as session:
        repo = MemoryReviewCandidateRepository(session, user_id=user_id)
        await repo.record_candidate(
            candidate_id=ids["conflict"], kind="conflicting", memory_id_a=a.memory.id,
            memory_id_b=b.memory.id, source_extraction_candidate_id=None,
            evidence={"reason": "same subject, predicate"}, score=0.9, now=now,
        )  # fmt: skip
        await repo.record_candidate(
            candidate_id=ids["duplicate"], kind="likely_duplicate", memory_id_a=c.memory.id,
            memory_id_b=d.memory.id, source_extraction_candidate_id=None,
            evidence={"similarity": 0.95}, score=0.95, now=now,
        )  # fmt: skip
        await repo.record_candidate(
            candidate_id=ids["same"], kind="likely_duplicate", memory_id_a=e.memory.id,
            memory_id_b=f.memory.id, source_extraction_candidate_id=None,
            evidence={"similarity": 0.97}, score=0.97, now=now,
        )  # fmt: skip
        merge = EntityMergeCandidateRepository(session, user_id=user_id)
        await merge.record_candidate(
            candidate_id=ids["merge"], entity_id_a=ent["OpenAI"], entity_id_b=ent["Open AI"],
            evidence={"normalized_name_match": True}, score=0.97, now=now,
        )  # fmt: skip
        await merge.record_candidate(
            candidate_id=ids["merge2"], entity_id_a=ent["Atlas"], entity_id_b=ent["OpenAI"],
            evidence={}, score=0.5, now=now,
        )  # fmt: skip
    ids["openai"] = ent["OpenAI"]
    return ids


async def test_review_routes_populated_contract(
    client: TestClient,
    db,
    graph,
    user_id,
    other_user_id,
    pg_cleanup,
    cleanup,  # noqa: ANN001
) -> None:
    ids = await _seed_review(db, graph, user_id)
    h = _h(user_id)

    memories = MemoryReviewCandidateListResponse.model_validate(
        client.get("/review/memories", headers=h).json()
    )
    assert {i.id for i in memories.items} >= {ids["conflict"], ids["duplicate"]}
    detail = MemoryReviewCandidateDetail.model_validate(
        client.get(f"/review/memories/{ids['conflict']}", headers=h).json()
    )
    assert detail.memory_a.memory.id == ids["mem_a"] and detail.status == "unreviewed"

    resolved = client.post(
        f"/review/memories/{ids['conflict']}/resolve-conflict",
        json={
            "if_version": detail.version,
            "resolution": "supersede",
            "winner_memory_id": ids["mem_a"],
        },
        headers=h,
    )
    assert resolved.status_code == 200, resolved.text
    assert MemoryReviewCandidateDetail.model_validate(resolved.json()).status == "conflict_resolved"
    # Repeating the SAME decision is an idempotent success ...
    repeat = client.post(
        f"/review/memories/{ids['conflict']}/resolve-conflict",
        json={
            "if_version": detail.version,
            "resolution": "supersede",
            "winner_memory_id": ids["mem_a"],
        },
        headers=h,
    )
    assert repeat.status_code == 200, repeat.text
    # ... but a CONTRADICTING one must not pretend to have applied.
    contradicting = client.post(
        f"/review/memories/{ids['conflict']}/resolve-conflict",
        json={"if_version": detail.version, "resolution": "keep_both"},
        headers=h,
    )
    assert contradicting.status_code == 409
    other_winner = client.post(
        f"/review/memories/{ids['conflict']}/resolve-conflict",
        json={
            "if_version": detail.version,
            "resolution": "supersede",
            "winner_memory_id": ids["mem_b"],
        },
        headers=h,
    )
    assert other_winner.status_code == 409

    dup = client.get(f"/review/memories/{ids['duplicate']}", headers=h).json()
    distinct = client.post(
        f"/review/memories/{ids['duplicate']}/confirm-distinct",
        json={"if_version": dup["version"], "reason": "different facts"},
        headers=h,
    )
    assert distinct.status_code == 200, distinct.text
    assert (
        MemoryReviewCandidateDetail.model_validate(distinct.json()).status == "confirmed_distinct"
    )

    same = client.get(f"/review/memories/{ids['same']}", headers=h).json()
    duplicate = client.post(
        f"/review/memories/{ids['same']}/confirm-duplicate",
        json={"if_version": same["version"], "reason": "same fact"},
        headers=h,
    )
    assert duplicate.status_code == 200, duplicate.text
    dup_body = MemoryReviewCandidateDetail.model_validate(duplicate.json())
    assert dup_body.status == "confirmed_duplicate"
    assert dup_body.memory_a.memory.status.value == "merged"

    entities = EntityMergeCandidateListResponse.model_validate(
        client.get("/review/entities", headers=h).json()
    )
    assert {i.id for i in entities.items} >= {ids["merge"], ids["merge2"]}
    merge = EntityMergeCandidateDetail.model_validate(
        client.get(f"/review/entities/{ids['merge']}", headers=h).json()
    )
    approved = client.post(
        f"/review/entities/{ids['merge']}/approve-merge",
        json={"if_version": merge.version, "canonical_entity_id": ids["openai"]},
        headers=h,
    )
    assert approved.status_code == 200, approved.text
    approved_body = EntityMergeCandidateDetail.model_validate(approved.json())
    assert approved_body.status == "merged"
    reversed_ = client.post(
        f"/review/entities/{ids['merge']}/reverse-merge",
        json={"if_version": approved_body.version, "reason": "wrong"},
        headers=h,
    )
    assert reversed_.status_code == 200, reversed_.text
    assert EntityMergeCandidateDetail.model_validate(reversed_.json()).status == "reversed"
    other = client.get(f"/review/entities/{ids['merge2']}", headers=h).json()
    rejected = client.post(
        f"/review/entities/{ids['merge2']}/reject-merge",
        json={"if_version": other["version"]},
        headers=h,
    )
    assert rejected.status_code == 200, rejected.text

    history = ReviewAuditLogResponse.model_validate(client.get("/review/history", headers=h).json())
    actions = {e.action for e in history.items}
    assert len(history.items) >= 5 and history.total >= 5
    assert actions, "the audit log (previously built by spreading rows) is populated"

    # Cross-user: another user sees none of it.
    o = _h(other_user_id)
    assert client.get("/review/memories", headers=o).json()["items"] == []
    assert client.get(f"/review/memories/{ids['duplicate']}", headers=o).status_code == 404
    assert client.get(f"/review/entities/{ids['merge2']}", headers=o).status_code == 404
    assert client.get("/review/history", headers=o).json()["items"] == []


async def test_extraction_job_routes_populated_contract(
    client: TestClient,
    db,
    graph,
    user_id,
    other_user_id,
    pg_cleanup,
    cleanup,  # noqa: ANN001
) -> None:
    from app.llm.fake import FakeLLMClient
    from app.models.extraction import ExtractionResult
    from tests.conftest import JAN

    settings = _settings()
    content = "I use Grafana at work every single day."
    await _message_job(db, settings, user_id, content)
    await _tick(
        db, graph, settings,
        FakeLLMClient(response=ExtractionResult(candidates=[_candidate(content, "Grafana")])),
        now=JAN,
    )  # fmt: skip

    listing = ExtractionJobListResponse.model_validate(
        client.get("/extraction/jobs", headers=_h(user_id)).json()
    )
    assert len(listing.items) == 1 and listing.items[0].status.value == "done"
    job_id = listing.items[0].id
    detail = ExtractionJobDetail.model_validate(
        client.get(f"/extraction/jobs/{job_id}", headers=_h(user_id)).json()
    )
    assert detail.job.candidate_count == 1 and len(detail.candidates) == 1
    assert client.get(f"/extraction/jobs/{job_id}", headers=_h(other_user_id)).status_code == 404
    assert client.get("/extraction/jobs", headers=_h(other_user_id)).json()["items"] == []


async def test_internal_routes_contract(client: TestClient, db, graph) -> None:  # noqa: ANN001
    t = {"X-Internal-Token": TOKEN}
    for path in ("/internal/extraction/sweep", "/internal/embeddings/backfill?max_users=1"):
        assert client.post(path).status_code == 401
    sweep = client.post("/internal/extraction/sweep", headers=t)
    assert sweep.status_code == 200, sweep.text
    SweepResult.model_validate(sweep.json())
    backfill = client.post("/internal/embeddings/backfill?max_users=1", headers=t)
    assert backfill.status_code == 200, backfill.text
    result = EmbeddingBackfillResult.model_validate(backfill.json())
    assert result.users_processed <= 1
    reconcile = client.post("/internal/review/reconcile", headers=t)
    assert reconcile.status_code == 200, reconcile.text


async def test_agent_turn_with_the_default_offline_provider(
    client: TestClient, db, graph, user_id, pg_cleanup, cleanup  # noqa: ANN001
) -> None:
    """Staging runs `LLM_PROVIDER=fake` (D7). Before Phase 11 this returned 500
    (unscripted FakeLLMClient, measured in M0); now an honest answer."""
    from app.llm.fake import OFFLINE_AGENT_ANSWER
    from app.schemas.agent import AgentTurnResponse

    conversation = client.post("/conversations", json={"title": "t"}, headers=_h(user_id))
    r = client.post(
        f"/conversations/{conversation.json()['id']}/agent-turn",
        json={"message": "What do I use for OCR these days at work?"},
        headers=_h(user_id),
    )
    assert r.status_code == 201, r.text
    body = AgentTurnResponse.model_validate(r.json())
    assert body.message.content == OFFLINE_AGENT_ANSWER
    assert body.cited_memory_ids == []
