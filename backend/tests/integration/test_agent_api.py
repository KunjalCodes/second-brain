"""The Phase 5 agent HTTP endpoint (docs/PHASE5_PLAN.md section 20 step 9),
against real Neo4j and real PostgreSQL. `search_memory`/`get_entity`/etc are
already proven correct in isolation by test_agent_tools.py; this file proves
the endpoint wires them together end-to-end: the user's message is
persisted, the agent loop runs against real retrieval, and the assistant's
answer is persisted and returned with a real, checkable citation.

The LLM itself is still a `FakeLLMClient` -- exercising a real provider is
Phase 5's separate `-m llm` suite, not this one.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_client, get_db, get_embeddings, get_llm
from app.embeddings.fake import FakeEmbeddingClient
from app.llm.fake import FakeLLMClient
from app.main import app
from app.models.agent import AgentStep, FinalAnswerStep, ToolCallStep
from app.models.conversation import MessageRole
from app.repositories.memory_repository import MemoryRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.integration

JAN = datetime(2026, 1, 15, tzinfo=UTC)


def _final(answer: str, cited: list[str]) -> AgentStep:
    return AgentStep(step=FinalAnswerStep(answer=answer, cited_memory_ids=cited))


def _tool_call(name: str, args: dict) -> AgentStep:
    return AgentStep(step=ToolCallStep(tool_name=name, tool_args_json=json.dumps(args)))


@pytest.fixture
async def client(db, graph):  # noqa: ANN001
    """The real app, with only the database/graph/LLM/embedding singletons
    swapped for this test's own connections and a scripted fake -- every
    other dependency (services, tools, the agent loop) runs unmodified.
    """
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient(dimension=768)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _create_conversation(client: AsyncClient, user_id: str) -> str:
    response = await client.post(
        "/conversations", json={"title": "agent test"}, headers={"X-User-Id": user_id}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


class TestAgentTurnEndpoint:
    async def test_answers_citing_a_real_retrieved_memory(
        self,
        client,
        db,
        graph,
        user_id,
        pg_cleanup,
        cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        memory_service = MemoryService(MemoryRepository(graph), now_fn=lambda: JAN)
        created = await memory_service.create(
            user_id=user_id,
            payload=MemoryCreate.model_validate(
                {
                    "content": "Drawing Extractor uses PaddleOCR",
                    "entities": [
                        {"name": "Drawing Extractor", "entity_type": "project"},
                        {"name": "PaddleOCR", "entity_type": "technology"},
                    ],
                    "relationships": [
                        {"source": "Drawing Extractor", "predicate": "uses", "target": "PaddleOCR"}
                    ],
                }
            ),
        )
        memory_id = created.memory.id

        app.dependency_overrides[get_llm] = lambda: FakeLLMClient(
            responses=[
                _tool_call("search_memory", {"query": "What do we know about PaddleOCR?"}),
                _final("You use PaddleOCR.", [memory_id]),
            ]
        )

        conversation_id = await _create_conversation(client, user_id)
        response = await client.post(
            f"/conversations/{conversation_id}/agent-turn",
            json={"message": "What OCR library do we use?"},
            headers={"X-User-Id": user_id},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["message"]["role"] == "assistant"
        assert body["message"]["content"] == "You use PaddleOCR."
        assert body["cited_memory_ids"] == [memory_id]
        assert body["tool_call_count"] == 1
        assert body["termination_reason"] == "final_answer"

        history = await client.get(
            f"/conversations/{conversation_id}", headers={"X-User-Id": user_id}
        )
        roles = [m["role"] for m in history.json()["messages"]]
        assert roles == ["user", "assistant"]

    async def test_answers_directly_with_no_tool_call_needed(
        self,
        client,
        db,
        user_id,
        pg_cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
        app.dependency_overrides[get_llm] = lambda: FakeLLMClient(
            response=_final("HNSW is an approximate nearest-neighbor index.", [])
        )

        conversation_id = await _create_conversation(client, user_id)
        response = await client.post(
            f"/conversations/{conversation_id}/agent-turn",
            json={"message": "What is HNSW?"},
            headers={"X-User-Id": user_id},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["tool_call_count"] == 0
        assert body["cited_memory_ids"] == []

    async def test_one_user_cannot_run_a_turn_on_anothers_conversation(
        self,
        client,
        db,
        user_id,
        other_user_id,
        pg_cleanup,  # noqa: ANN001
    ) -> None:
        async with db.session() as session:
            await UserRepository(session).ensure_exists(user_id, now=JAN)
            await UserRepository(session).ensure_exists(other_user_id, now=JAN)
        app.dependency_overrides[get_llm] = lambda: FakeLLMClient(response=_final("irrelevant", []))

        conversation_id = await _create_conversation(client, user_id)
        response = await client.post(
            f"/conversations/{conversation_id}/agent-turn",
            json={"message": "hi"},
            headers={"X-User-Id": other_user_id},
        )
        assert response.status_code == 404

    async def test_window_is_the_newest_messages_of_a_long_conversation(
        self,
        client,
        db,
        user_id,
        pg_cleanup,  # noqa: ANN001
    ) -> None:
        """Past 1000 messages the agent used to see messages 991-1000 -- never
        the user's new question (Phase 11 re-verification)."""
        conversation_id = await _create_conversation(client, user_id)
        async with db.session() as session:
            repo = MessageRepository(session, user_id=user_id)
            for i in range(1005):
                await repo.append(
                    message_id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role=MessageRole.USER,
                    content=f"filler message number {i}",
                    token_count=None,
                    now=JAN,
                )
        fake = FakeLLMClient(response=_final("ok", []))
        app.dependency_overrides[get_llm] = lambda: fake

        response = await client.post(
            f"/conversations/{conversation_id}/agent-turn",
            json={"message": "the newest question"},
            headers={"X-User-Id": user_id},
        )
        assert response.status_code == 201, response.text
        prompt = fake.calls[0]["prompt"]
        # Window = 10 newest: the question plus fillers 996..1004.
        shown = {int(n) for n in re.findall(r"filler message number (\d+)\b", prompt)}
        assert "the newest question" in prompt
        assert shown == set(range(996, 1005))
