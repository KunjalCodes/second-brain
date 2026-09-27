"""Request body size limits, enforced before any parsing (Phase 11, M6/S10).

Before Phase 11 an upload was parsed (multipart spooled to a temp file) and
then read fully into memory by the route before its size was checked, so an
oversized request cost its full size in memory -- and parsing ran before
authentication. This pure ASGI middleware bounds every request body:

* a declared `Content-Length` above the limit is refused with 413 before a
  single body byte is read;
* a body without `Content-Length` (chunked) is counted as it streams, and
  the request is aborted with 413 as soon as the running total passes the
  limit -- nothing beyond limit + one chunk is ever buffered.

Limits: `document_upload_limit` for `POST .../documents` (the document size
limit plus multipart framing overhead), `default_limit` for everything else.
The route still validates the exact file size (`validate_upload`); this layer
only guarantees that nothing unbounded reaches it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Room for multipart boundaries and part headers around the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class _BodyTooLarge(Exception):
    pass


def _payload(limit: int) -> bytes:
    return json.dumps(
        {
            "error": {
                "code": "request_too_large",
                "message": f"The request body exceeds the limit of {limit} bytes",
            }
        }
    ).encode()


class BodySizeLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, default_limit: int, document_upload_limit: int
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.document_upload_limit = document_upload_limit

    def _limit_for(self, scope: Scope) -> int:
        path = scope.get("path", "").rstrip("/")
        if scope.get("method") == "POST" and path.endswith("/documents"):
            return self.document_upload_limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False  # a malformed header is the server's parser's problem
            if too_big:
                await self._reject(send, limit)
                return

        received = 0
        started = False
        overflowed = False

        async def counting_receive() -> Message:
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    overflowed = True
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if overflowed:
                # A layer below turned the overflow into its own response
                # (e.g. FastAPI's "error parsing the body" 400): replace it.
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await self._reject(send, limit)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = _payload(limit)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
