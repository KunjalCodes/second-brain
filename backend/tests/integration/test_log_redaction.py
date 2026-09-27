"""Phase 11 (M5): nothing sensitive reaches the logs, in the real JSON format.

Drives login (right and wrong password), cookie + bearer requests, a memory
search, a ranked search, a document upload and an agent turn, capturing EVERY
record at DEBUG through the production `JsonFormatter` (so structured extras
are included), then asserts that none of the sensitive values appear.
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging import JsonFormatter, _ContextFilter
from tests.integration.test_auth import ORIGIN, PASSWORD, Harness, h  # noqa: F401

pytestmark = pytest.mark.integration

SEARCH_TEXT = "my-private-diagnosis-term"
DOC_TEXT = "Confidential clause about the Atlas merger, never log this sentence."
AGENT_TEXT = "Tell me about my confidential Atlas merger plans please."


async def test_sensitive_values_never_appear_in_json_logs(h: Harness) -> None:  # noqa: F811
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Capture(level=logging.DEBUG)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        user_id, email = await h.account()
        async with h.client() as c:
            token = await h.login(c, email)
            bearer = {"Authorization": f"Bearer {token}"}
            await c.post("/auth/login", json={"email": email, "password": "wrong password here"})
            # Cookie-authenticated request (session cookie from a normal login).
            r = await c.post(
                "/auth/login",
                json={"email": email, "password": PASSWORD},
                headers={"Origin": ORIGIN},
            )
            cookie = r.cookies.get("sb_session")
            await c.get("/auth/me")
            await c.get("/memories/search", params={"q": SEARCH_TEXT}, headers=bearer)
            await c.post("/search", json={"query": SEARCH_TEXT}, headers=bearer)
            await c.post(
                "/documents",
                files={"file": ("notes.txt", DOC_TEXT.encode(), "text/plain")},
                headers=bearer,
            )
            conv = await c.post("/conversations", json={"title": "t"}, headers=bearer)
            await c.post(
                f"/conversations/{conv.json()['id']}/agent-turn",
                json={"message": AGENT_TEXT},
                headers=bearer,
            )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    logged = "\n".join(lines)
    assert lines, "the flows above must have produced log output"
    for secret in (
        token,
        cookie,
        PASSWORD,
        "wrong password here",
        email,
        SEARCH_TEXT,
        DOC_TEXT,
        AGENT_TEXT,
    ):
        assert secret and secret not in logged, f"leaked into logs: {secret[:20]}..."
    assert "Bearer " not in logged and "sb_session=" not in logged
    # Correlation is present: access lines carry the user id and request id.
    assert f'"user_id": "{user_id}"' in logged
    assert '"event": "http_request"' in logged
