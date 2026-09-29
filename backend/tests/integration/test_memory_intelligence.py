"""Phase 14 memory-intelligence behaviour, against real Neo4j and PostgreSQL.

Each test pins one defect found by the M0 audit (docs/PHASE14_MEMORY_MODEL.md)
or one new capability. The LLM is scripted; everything else is real.
"""

# ruff: noqa: E501 - scripted candidates are kept one per line for readability

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.deps import get_client, get_db, get_embeddings
from app.embeddings.fake import FakeEmbeddingClient
from app.extraction.runner import run_worker_tick
from app.extraction.trigger import maybe_trigger_extraction
from app.llm.fake import FakeLLMClient
from app.main import app
from app.models.conversation import MessageRole
from app.models.extraction import (
    CandidateEntity,
    CandidateMemory,
    CandidateRelationship,
    CandidateTemporal,
    ExtractionResult,
    Span,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.entity_repository import EntityRepository
from app.repositories.extraction_repository import ExtractionJobRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.integration

JAN10 = datetime(2026, 1, 10, 9, 0, tzinfo=UTC)
FEB01 = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
MAY05 = datetime(2026, 5, 5, 9, 0, tzinfo=UTC)
JUN01 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_TARGET = re.compile(r"Target message id: (\S+)")


def _native(value):  # noqa: ANN001, ANN202
    return value.to_native() if hasattr(value, "to_native") else value


def _cand(message_id: str, content: str, statement: str, rels, *, ents, **temporal):  # noqa: ANN001
    return CandidateMemory(
        statement=statement,
        memory_type="semantic",
        persistence="persistent",
        confidence=0.9,
        entities=[CandidateEntity(name=n, entity_type=t, mention_span=None) for n, t in ents],
        relationships=[CandidateRelationship(source=a, predicate=p, target=b) for a, p, b in rels],
        temporal=CandidateTemporal(**temporal),
        source_message_id=message_id,
        source_span=Span(start=0, end=len(content)),
        reasoning="scripted",
    )


class Chat:
    """One user's conversation, with a scripted LLM answering per target message."""

    def __init__(self, db, graph, settings, user_id: str) -> None:  # noqa: ANN001
        self.db, self.graph, self.settings, self.uid = db, graph, settings, user_id
        self.by_target: dict[str, list[CandidateMemory]] = {}
        self.llm = FakeLLMClient(respond_fn=self._respond)
        self.conv_id: str | None = None

    def _respond(self, prompt: str) -> ExtractionResult:
        m = _TARGET.search(prompt)
        return ExtractionResult(candidates=self.by_target.get(m.group(1), []) if m else [])

    async def say(self, content: str, at: datetime, build) -> str:  # noqa: ANN001
        async with self.db.session() as s:
            await UserRepository(s).ensure_exists(self.uid, now=at)
            if self.conv_id is None:
                conv = await ConversationRepository(s, user_id=self.uid).create(
                    conversation_id=str(uuid.uuid4()), title="t", now=at
                )
                self.conv_id = conv.id
            repo = MessageRepository(s, user_id=self.uid)
            msg = await repo.append(
                message_id=str(uuid.uuid4()), conversation_id=self.conv_id,
                role=MessageRole.USER, content=content, token_count=None, now=at,
            )  # fmt: skip
            await maybe_trigger_extraction(
                message=msg, message_repo=repo,
                job_repo=ExtractionJobRepository(s, user_id=self.uid),
                settings=self.settings, now=at,
            )  # fmt: skip
        self.by_target[msg.id] = build(msg.id, content)
        return msg.id

    async def process(self, at: datetime) -> None:
        for _ in range(5):
            n = await run_worker_tick(
                db=self.db, graph_client=self.graph, llm_client=self.llm,
                embedding_client=FakeEmbeddingClient(), settings=self.settings,
                worker_id="t", limit=10, now_fn=lambda: at,
            )  # fmt: skip
            if n == 0:
                return

    async def memory(self, content: str) -> dict:
        rows = await self.graph.read(
            "MATCH (m:Memory {user_id: $u, content: $c}) "
            "RETURN m.status AS status, m.valid_from AS vf, m.valid_until AS vu, m.id AS id",
            {"u": self.uid, "c": content},
        )
        assert rows, f"memory {content!r} was not stored"
        r = rows[0]
        return {
            "id": r["id"], "status": r["status"],
            "valid_from": _native(r["vf"]),
            "valid_until": _native(r["vu"]),
        }  # fmt: skip


@pytest.fixture
def settings():  # noqa: ANN201
    from app.core.config import get_settings

    return get_settings().model_copy(update={"extraction_enabled": True})


@pytest.fixture
def chat(db, graph, settings, user_id) -> Chat:  # noqa: ANN001
    return Chat(db, graph, settings, user_id)


async def test_a_burst_of_messages_extracts_every_message(chat, db, pg_cleanup, cleanup):  # noqa: ANN001
    """The debounce used to retarget one job to the newest message, leaving the
    first message `queued` forever and its facts unextracted."""
    a = "Atlas is hosted on Render."
    b = "Priya Shah tests Cutlist Optimizer."
    await chat.say(a, JAN10, lambda mid, c: [_cand(mid, c, a, [["Atlas", "hosted_on", "Render"]],
                   ents=[["Atlas", "project"], ["Render", "technology"]])])  # fmt: skip
    await chat.say(b, JAN10, lambda mid, c: [_cand(mid, c, b, [["Priya Shah", "tests", "Cutlist Optimizer"]],
                   ents=[["Priya Shah", "person"], ["Cutlist Optimizer", "project"]])])  # fmt: skip
    await chat.process(JAN10)
    async with db.session() as s:
        statuses = [r[0] for r in await s.execute(
            text("SELECT extraction_status FROM messages WHERE conversation_id = :c ORDER BY sequence"),
            {"c": chat.conv_id},
        )]  # fmt: skip
    assert statuses == ["done", "done"]
    await chat.memory(a)
    await chat.memory(b)


async def test_valid_time_is_when_it_was_said_not_when_it_was_processed(chat, pg_cleanup, cleanup):  # noqa: ANN001
    old, new = "Drawing Extractor uses EasyOCR.", "Drawing Extractor uses PaddleOCR."
    ents_old = [["Drawing Extractor", "project"], ["EasyOCR", "technology"]]
    ents_new = [["Drawing Extractor", "project"], ["PaddleOCR", "technology"]]
    await chat.say("Drawing Extractor uses EasyOCR.", JAN10, lambda mid, c: [
        _cand(mid, c, old, [["Drawing Extractor", "uses", "EasyOCR"]], ents=ents_old)])  # fmt: skip
    await chat.process(FEB01)  # processed three weeks late
    await chat.say("Drawing Extractor switched from EasyOCR to PaddleOCR.", MAY05, lambda mid, c: [
        _cand(mid, c, new, [["Drawing Extractor", "uses", "PaddleOCR"]], ents=ents_new,
              is_change=True, replaces_hint="EasyOCR")])  # fmt: skip
    await chat.process(JUN01)
    before, after = await chat.memory(old), await chat.memory(new)
    assert before["valid_from"] == JAN10
    assert before["status"] == "superseded"
    assert before["valid_until"] == MAY05  # when the change was said, not processed
    assert after["valid_from"] == MAY05


async def test_temporal_hints_set_the_window(chat, pg_cleanup, cleanup):  # noqa: ANN001
    since, until = "Atlas uses Grafana.", "Atlas was hosted on Heroku."
    await chat.say("Atlas has used Grafana since March 2025.", JUN01, lambda mid, c: [
        _cand(mid, c, since, [["Atlas", "uses", "Grafana"]],
              ents=[["Atlas", "project"], ["Grafana", "technology"]],
              valid_from_hint="since March 2025")])  # fmt: skip
    await chat.say("Atlas used Heroku until May 2026.", JUN01, lambda mid, c: [
        _cand(mid, c, until, [["Atlas", "hosted_on", "Heroku"]],
              ents=[["Atlas", "project"], ["Heroku", "technology"]],
              valid_until_hint="until May 2026")])  # fmt: skip
    await chat.process(JUN01)
    assert (await chat.memory(since))["valid_from"] == datetime(2025, 3, 1, tzinfo=UTC)
    past = await chat.memory(until)
    assert past["valid_until"] == datetime(2026, 5, 1, tzinfo=UTC)
    assert past["valid_from"] is None  # start unknown -- never an inverted window


async def test_a_write_naming_a_merged_away_entity_lands_on_the_canonical_one(
    graph,
    user_id,
    cleanup,  # noqa: ANN001
) -> None:
    ms = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN10)

    def mc(content: str, name: str) -> MemoryCreate:
        return MemoryCreate.model_validate({
            "content": content,
            "entities": [{"name": "Atlas", "entity_type": "project"},
                         {"name": name, "entity_type": "company"}],
            "relationships": [{"source": "Atlas", "predicate": "uses", "target": name}],
        })  # fmt: skip

    a = await ms.create(user_id=user_id, payload=mc("Atlas uses OpenAI.", "OpenAI"))
    b = await ms.create(user_id=user_id, payload=mc("Atlas uses Open AI models.", "Open AI"))
    canonical = next(e.id for e in a.entities if e.name == "OpenAI")
    merged = next(e.id for e in b.entities if e.name == "Open AI")
    await EntityRepository(graph).merge(
        user_id=user_id, entity_id_a=canonical, entity_id_b=merged, merge_id="m1",
        new_aliases=["Open AI"], excluded_statuses=[], now=JAN10, id_fn=lambda: str(uuid.uuid4()),
    )  # fmt: skip
    after = await ms.create(
        user_id=user_id, payload=mc("Atlas uses Open AI embeddings.", "Open AI")
    )
    assert {e.id for e in after.entities} == {
        canonical,
        next(e.id for e in a.entities if e.name == "Atlas"),
    }
    canonical_entity = next(e for e in after.entities if e.id == canonical)
    assert "open ai" in canonical_entity.aliases


async def test_explanation_endpoint(chat, db, graph, user_id, other_user_id, pg_cleanup, cleanup):  # noqa: ANN001
    statement = "Remember that Atlas deploys on Tuesdays."
    await chat.say(statement, JUN01, lambda mid, c: [
        _cand(mid, c, statement, [], ents=[["Atlas", "project"]], valid_from_hint="since 2025")
        .model_copy(update={"persistence": "temporary"})])  # fmt: skip
    await chat.process(JUN01)
    mem = await chat.memory(statement)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/memories/{mem['id']}/explanation", headers={"X-User-Id": user_id})
            other = await c.get(
                f"/memories/{mem['id']}/explanation", headers={"X-User-Id": other_user_id}
            )
            direct = await c.post(
                "/memories", json={"content": "Typed in directly."}, headers={"X-User-Id": user_id}
            )
            d = await c.get(
                f"/memories/{direct.json()['memory']['id']}/explanation",
                headers={"X-User-Id": user_id},
            )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origin"] == "extraction"
    [decision] = body["decisions"]
    assert decision["decision"] == "NEW"
    assert decision["role"] == "creation"
    # stored even though the model said "temporary": the user asked for it
    assert decision["worthiness_rule"] == "explicit_user_instruction"
    assert decision["persistence"] == "temporary"
    assert "2025" in (decision["temporal_interpretation"] or "")
    assert decision["model_reasoning"] == "scripted"
    assert body["sources"][0]["resolved"] is True
    assert "not a probability" in body["confidence_note"]
    assert other.status_code == 404  # isolation
    assert d.status_code == 200 and d.json()["origin"] == "direct" and d.json()["decisions"] == []


async def test_graph_mentions_are_whole_tokens_and_keyword_matches_terms(
    db,
    graph,
    user_id,
    cleanup,
    pg_cleanup,  # noqa: ANN001
) -> None:
    from app.core.config import get_settings
    from app.services.hybrid_retrieval_service import HybridRetrievalService
    from app.services.retrieval_service import RetrievalService

    ms = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN10)
    ai = await ms.create(user_id=user_id, payload=MemoryCreate.model_validate(
        {"content": "AI features are planned for Q3.", "entities": [{"name": "AI", "entity_type": "concept"}]}
    ))  # fmt: skip
    deploy = await ms.create(user_id=user_id, payload=MemoryCreate.model_validate(
        {"content": "The team deploys new releases every Tuesday."}
    ))  # fmt: skip
    async with db.session() as s:
        hy = HybridRetrievalService(
            session=s, memory_service=ms,
            retrieval_service=RetrievalService(EntityRepository(graph), max_hops=2),
            embedding_client=FakeEmbeddingClient(), settings=get_settings(), enable_vector=False,
        )  # fmt: skip
        said = [
            r.memory.memory.id
            for r in await hy.search(user_id=user_id, query="Who said the build failed?")
        ]
        when = [
            r.memory.memory.id
            for r in await hy.search(user_id=user_id, query="When do deployments happen?")
        ]
    assert ai.memory.id not in said  # "ai" inside "said" is not a mention
    assert deploy.memory.id in when  # "deployments" ~ "deploys"
