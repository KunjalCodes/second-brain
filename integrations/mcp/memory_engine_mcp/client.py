"""A minimal, safe HTTP client for the memory engine.

* The ONLY credential is a project token (`sbp_...`) read from the environment.
* Every call goes to `/projects/current/...`: the token IS the project, so no tool can
  name a project, a user or a space.
* Errors are reduced to a short code and message. The token, headers and raw bodies are
  never included in anything returned to the agent or logged.
* Responses are size-capped before they reach the agent's context.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 120_000
TOKEN_RE = re.compile(r"^sbp_[A-Za-z0-9_-]{43}$")


class EngineError(Exception):
    """Safe to show to an agent: a status, a stable code and the engine's message."""

    def __init__(self, status: int, code: str, message: str, fields: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message, self.fields = status, code, message, fields


def _field_hints(err: dict[str, Any]) -> str:
    """The engine's field-level validation errors (`details.fields`), as short text, so an
    agent can correct its call instead of guessing. Only field names and messages."""
    fields = (err.get("details") or {}).get("fields")
    if not isinstance(fields, list):
        return ""
    parts = [f"{str(f.get('field', ''))[:80]}: {str(f.get('message', ''))[:140]}" for f in fields[:6] if isinstance(f, dict)]
    return "; ".join(parts)


class EngineClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("MEMORY_ENGINE_URL must be an http(s) URL")
        if not TOKEN_RE.match(token):
            raise ValueError("MEMORY_ENGINE_TOKEN is not a project token (sbp_...)")
        self._http = httpx.AsyncClient(
            base_url=f"{parts.scheme}://{parts.netloc}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # never forward the token to another host
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def request(
        self, method: str, route: str, *, params: dict[str, Any] | None = None, body: Any = None  # noqa: ANN401
    ) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(
                method, f"/projects/current{route}", params=clean, json=body
            )
        except httpx.HTTPError as exc:
            raise EngineError(0, "unreachable", f"could not reach the memory engine ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
                raise EngineError(resp.status_code, str(err.get("code", "error"))[:60], str(err.get("message", ""))[:300], _field_hints(err))
            except (ValueError, KeyError, TypeError):
                raise EngineError(resp.status_code, "error", "the memory engine returned an error") from None
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise EngineError(502, "response_too_large", "the engine response exceeded the adapter's size cap")
        try:
            return json.loads(resp.content)
        except ValueError:
            raise EngineError(502, "bad_response", "the memory engine returned a non-JSON response") from None

    async def get(self, route: str, **params: Any) -> dict[str, Any]:  # noqa: ANN401
        return await self.request("GET", route, params=params)

    async def post(self, route: str, body: Any) -> dict[str, Any]:  # noqa: ANN401
        return await self.request("POST", route, body=body)
