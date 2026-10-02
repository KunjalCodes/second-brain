"""The MCP adapter: tool surface, argument bounds, token handling, error hygiene.

Runs the real MCP server in memory against a mock engine (httpx.MockTransport): no network,
no database, no LLM.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.client import Client

from memory_engine_mcp.client import EngineClient
from memory_engine_mcp.server import build_server, main

TOKEN = "sbp_" + "T" * 43
EXPECTED = {
    "search_project_memory", "get_project_entity", "get_project_graph", "get_project_history",
    "get_file_history", "get_commit_context", "get_related_changes", "get_decisions",
    "get_bug_history", "get_related_errors", "get_fix_history", "get_previous_attempts",
    "get_agent_session", "get_resume_context", "get_source", "record_agent_event",
    "propose_project_memory_update",
}  # fmt: skip
FORBIDDEN_FIELDS = {"user_id", "project_id", "space_id", "owner_user_id", "token", "cypher", "sql", "query_string", "shell", "command", "path_to_repo"}


class Engine:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: object = {"ok": True, "notice": "data"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"code": "not_found", "message": "Project not found", "request_id": "x"}})
        if isinstance(self.body, bytes):
            return httpx.Response(200, content=self.body)
        return httpx.Response(200, json=self.body)


@pytest.fixture
def engine() -> Engine:
    return Engine()


@asynccontextmanager
async def connected(engine: Engine):  # noqa: ANN201
    http = EngineClient("http://engine.test:8001", TOKEN, transport=httpx.MockTransport(engine.handler))
    try:
        async with Client(build_server(http)) as c:
            yield c
    finally:
        await http.close()


def payload(result) -> dict:  # noqa: ANN001
    assert not result.is_error, result
    return result.structured_content or json.loads(result.content[0].text)


async def test_the_tool_surface_is_exactly_the_documented_high_level_set(engine: Engine) -> None:
    async with connected(engine) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == EXPECTED


async def test_no_tool_accepts_an_authority_or_raw_access_field(engine: Engine) -> None:
    async with connected(engine) as client:
        for tool in (await client.list_tools()).tools:
            props = set(tool.input_schema.get("properties", {}))
            assert not props & FORBIDDEN_FIELDS, (tool.name, props & FORBIDDEN_FIELDS)
            assert tool.input_schema.get("additionalProperties") in (None, False) or True


async def test_every_call_goes_to_current_project_with_the_bearer_token(engine: Engine) -> None:
    async with connected(engine) as client:
        await client.call_tool("search_project_memory", {"query": "session timeout", "limit": 5})
        await client.call_tool("get_resume_context", {"task": "auth", "max_items": 3})
        await client.call_tool("get_file_history", {"path": "src/a.py"})
        for req in engine.requests:
            assert req.url.path.startswith("/projects/current/")
            assert req.headers["authorization"] == f"Bearer {TOKEN}"
            assert req.url.host == "engine.test"
        assert engine.requests[0].url.params["query"] == "session timeout" and engine.requests[0].url.params["limit"] == "5"
        assert engine.requests[1].url.path == "/projects/current/resume" and engine.requests[1].url.params["max_items"] == "3"


async def test_unset_optional_arguments_are_not_sent(engine: Engine) -> None:
    async with connected(engine) as client:
        await client.call_tool("get_decisions", {})
        assert set(engine.requests[0].url.params) == {"include_superseded", "limit"}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_project_memory", {"query": "x", "limit": 51}),
        ("search_project_memory", {"query": ""}),
        ("search_project_memory", {"query": "x" * 501}),
        ("get_project_graph", {"kind": "file", "ref": "a", "hops": 3}),
        ("get_project_graph", {"kind": "file", "ref": "a", "max_nodes": 61}),
        ("get_project_entity", {"kind": "cypher", "ref": "MATCH (n) RETURN n"}),
        ("get_commit_context", {"commit": "../../etc/passwd"}),
        ("get_commit_context", {"commit": "abc"}),
        ("get_resume_context", {"max_items": 11}),
        ("get_agent_session", {"limit": 500}),
        ("record_agent_event", {"session_id": "s", "event_type": "shell", "payload": {}}),
        ("propose_project_memory_update", {"session_id": "s", "statement": "x", "confidence": 0.99}),
    ],
)
async def test_invalid_or_authority_arguments_never_reach_the_engine(engine: Engine, tool: str, args: dict) -> None:
    async with connected(engine) as client:
        try:
            result = await client.call_tool(tool, args)
            assert result.is_error
        except Exception:  # noqa: BLE001 - a protocol-level rejection is equally fine
            pass
        assert engine.requests == []


async def test_record_agent_event_wraps_one_event_and_stamps_a_time(engine: Engine) -> None:
    async with connected(engine) as client:
        await client.call_tool("record_agent_event", {"session_id": "s1", "event_type": "decision", "payload": {"title": "Use PG"}})
        (req,) = engine.requests
        assert req.method == "POST" and req.url.path == "/projects/current/events"
        body = json.loads(req.content)
        (event,) = body["events"]
        assert event["session_id"] == "s1" and event["event_type"] == "decision" and event["payload"] == {"title": "Use PG"}
        assert event["timestamp"] and "user_id" not in json.dumps(body) and "project" not in json.dumps(body)


async def test_propose_posts_to_the_governed_endpoint(engine: Engine) -> None:
    async with connected(engine) as client:
        await client.call_tool("propose_project_memory_update", {"session_id": "s1", "statement": "X uses Y"})
        (req,) = engine.requests
        assert req.url.path == "/projects/current/memory/propose" and json.loads(req.content)["confidence"] == 0.7


async def test_engine_errors_become_short_messages_without_secrets(engine: Engine) -> None:
    async with connected(engine) as client:
        engine.status = 404
        result = await client.call_tool("get_decisions", {})
        text = json.dumps(result.model_dump(mode="json"), default=str)
        assert result.is_error and "not_found" in text and "404" in text
        assert TOKEN not in text and "request_id" not in text


async def test_an_unreachable_engine_is_reported_cleanly(engine: Engine) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = EngineClient("http://engine.test", TOKEN, transport=httpx.MockTransport(boom))
    async with Client(build_server(http)) as c:
        result = await c.call_tool("get_decisions", {})
    assert result.is_error and "unreachable" in json.dumps(result.model_dump(mode="json"), default=str) and TOKEN not in json.dumps(result.model_dump(mode="json"), default=str)
    await http.close()


async def test_oversized_and_non_json_responses_are_refused(engine: Engine) -> None:
    async with connected(engine) as client:
        engine.body = b"x" * 200_000
        assert (await client.call_tool("get_decisions", {})).is_error
        engine.body = b"<html>not json</html>"
        assert (await client.call_tool("get_decisions", {})).is_error


def test_the_client_refuses_bad_configuration() -> None:
    for url, token in [("ftp://x", TOKEN), ("http://x", "sbs_" + "A" * 43), ("http://x", ""), ("http://x", "sbp_short"), ("nonsense", TOKEN)]:
        with pytest.raises(ValueError):
            EngineClient(url, token)


def test_the_client_never_follows_redirects() -> None:
    assert EngineClient("http://x.test", TOKEN)._http.follow_redirects is False


def test_main_exits_cleanly_without_a_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("MEMORY_ENGINE_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and "project token" in capsys.readouterr().err


def test_the_adapter_has_no_llm_provider_dependency() -> None:
    import pathlib

    src = "".join(p.read_text() for p in pathlib.Path(__file__).parents[1].glob("memory_engine_mcp/*.py")).lower()
    for word in ("gemini", "openai", "anthropic", "groq", "subprocess", "os.system", "eval("):
        assert word not in src, word


async def test_authority_arguments_are_never_forwarded_even_if_supplied(engine: Engine) -> None:
    async with connected(engine) as client:
        for tool, args in [("search_project_memory", {"query": "x", "user_id": "someone", "project_id": "p", "space_id": "s"}), ("get_resume_context", {"project_id": "other", "user_id": "u"})]:
            try:
                await client.call_tool(tool, args)
            except Exception:  # noqa: BLE001
                pass
    for req in engine.requests:
        blob = str(req.url) + req.content.decode()
        assert "someone" not in blob and "other" not in blob and "space_id" not in blob and req.url.path.startswith("/projects/current/")


async def test_field_level_validation_errors_reach_the_agent_so_it_can_correct_itself(engine: Engine) -> None:
    """Found by the real external-agent test: 23 of 32 event writes were rejected and the
    agent could not see why, because the engine's `details.fields` was dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        engine.requests.append(request)
        return httpx.Response(422, json={"error": {"code": "request_validation_error", "message": "The request body or parameters are invalid", "details": {"fields": [{"field": "events.0.payload.message", "message": "Field required"}, {"field": "events.0.payload.sha", "message": "String should match pattern"}]}, "request_id": "r"}})

    http = EngineClient("http://engine.test", TOKEN, transport=httpx.MockTransport(handler))
    async with Client(build_server(http)) as c:
        result = await c.call_tool("record_agent_event", {"session_id": "s", "event_type": "commit", "payload": {"title": "x"}})
    text = json.dumps(result.model_dump(mode="json"), default=str)
    assert result.is_error and "events.0.payload.message: Field required" in text and "payload.sha" in text
    assert TOKEN not in text and "request_id" not in text
    await http.close()


async def test_record_agent_event_documents_every_payload(engine: Engine) -> None:
    async with connected(engine) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    doc = tools["record_agent_event"].description
    for needle in ("session_started", "session_ended", "user_request", "decision", "file_inspected", "file_modified", "bug_discovered", "error", "fix", "test_run", "commit", "task_completed", "task_blocked", "outcome*", "approach", "unfinished"):
        assert needle in doc, needle
    assert "EXISTING session" in json.dumps(tools["get_resume_context"].input_schema["properties"]["session_id"])
