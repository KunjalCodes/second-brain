# ruff: noqa: E501, F811
"""Phase 17 regressions: defects the system audit reproduced and fixed.

* concurrent single-commit Git requests used to lose commit memories (uniqueness race
  -> terminal `skipped`);
* an agent turn with AGENT_WRITE_ENABLED=true used to deadlock on the conversation row lock
  (the tool opens its own session while the route held the user's message uncommitted).
"""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.test_project_git import GitWorld, g  # noqa: F401
from tests.integration.test_projects import h  # noqa: F401
from tests.unit.test_git_collector import gc, repo  # noqa: F401

pytestmark = pytest.mark.integration


async def test_concurrent_single_commit_requests_all_project(g: GitWorld) -> None:
    """Every commit sent by its own concurrent request is projected, none is dropped."""
    commits, branches = gc.collect_commits(str(g.repo), 50)
    reqs = [g.c.post(f"/projects/{g.project_id}/git/commits", json={"identity": g.identity(), "branches": branches, "commits": [c]}, headers=g.token) for c in commits]
    rs = await asyncio.gather(*reqs)
    statuses = [(r.status_code, r.json()["results"][0]["projection_status"], r.json()["results"][0]["note"]) for r in rs]
    assert all(s[1] == "projected" for s in statuses), statuses


async def test_agent_turn_can_write_a_memory_through_the_live_route(db, graph, monkeypatch) -> None:  # noqa: ANN001
    """AGENT_WRITE_ENABLED=true through the REAL route (the user's message is not committed
    when the tool opens its own session)."""
    import json
    import uuid
    from datetime import UTC, datetime

    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_client, get_db, get_embeddings, get_llm
    from app.core.config import get_settings
    from app.embeddings.fake import FakeEmbeddingClient
    from app.llm.fake import FakeLLMClient
    from app.main import app
    from app.models.agent import AgentStep, FinalAnswerStep, ToolCallStep
    from app.repositories.user_repository import UserRepository

    monkeypatch.setenv("AGENT_WRITE_ENABLED", "true")
    get_settings.cache_clear()
    uid = f"p17-repro-{uuid.uuid4().hex[:8]}"
    async with db.session() as s:
        await UserRepository(s).ensure_exists(uid, now=datetime.now(UTC))
    args = {"statement": "Kunjal uses Neovim", "memory_type": "semantic", "persistence": "persistent", "confidence": 0.9,
            "entities": [{"name": "Kunjal", "entity_type": "person"}, {"name": "Neovim", "entity_type": "technology"}],
            "relationships": [{"source": "Kunjal", "predicate": "uses", "target": "Neovim"}], "reasoning": "stated by the user"}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient(dimension=768)
    app.dependency_overrides[get_llm] = lambda: FakeLLMClient(responses=[
        AgentStep(step=ToolCallStep(tool_name="propose_memory_update", tool_args_json=json.dumps(args))),
        AgentStep(step=FinalAnswerStep(answer="Noted.", cited_memory_ids=[])),
    ])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            conv = (await c.post("/conversations", json={"title": "x"}, headers={"X-User-Id": uid})).json()["id"]
            r = await c.post(f"/conversations/{conv}/agent-turn", json={"message": "Remember that Kunjal uses Neovim for editing."}, headers={"X-User-Id": uid})
            rows = await graph.read("MATCH (m:Memory {user_id:$u}) RETURN count(m) AS n", {"u": uid})
            assert r.status_code == 201 and rows[0]["n"] >= 1
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        from sqlalchemy import text as t
        async with db.session() as s:
            for tbl in ("review_audit_log", "memory_review_candidates", "extraction_candidates", "extraction_jobs", "memory_embeddings", "messages", "conversations"):
                await s.execute(t(f"DELETE FROM {tbl} WHERE user_id=:u"), {"u": uid})  # noqa: S608
            await s.execute(t("DELETE FROM users WHERE id=:u"), {"u": uid})
        await graph.write("MATCH (n {user_id:$u}) DETACH DELETE n", {"u": uid})
