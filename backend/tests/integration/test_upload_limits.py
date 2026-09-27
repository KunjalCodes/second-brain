"""Phase 11 (M6/S10): bounded uploads.

The size limit is enforced before the body is parsed or fully read:
declared `Content-Length` over the limit -> 413 immediately; a streamed
body without `Content-Length` -> 413 as soon as the running total passes the
limit. Also: exactly-at-limit is accepted, malformed content and a truncated
multipart body fail cleanly, and an oversized upload does not allocate its
size in memory (tracemalloc).
"""

from __future__ import annotations

import tracemalloc
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text

from app.api.deps import get_client, get_db
from app.core.config import get_settings
from app.main import create_app

pytestmark = pytest.mark.integration

USER = "itest-upload-limits"
LIMIT = 64 * 1024


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, db, graph, tmp_path
) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ANN001
    monkeypatch.setenv("MAX_DOCUMENT_SIZE_BYTES", str(LIMIT))
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    app = create_app()
    # httpx's ASGI transport does not run the lifespan: use the test's
    # connected clients, like tests/integration/test_auth.py's harness.
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    async with db.session() as session:
        for stmt in (
            "DELETE FROM extraction_jobs WHERE user_id = :u",
            "DELETE FROM ingestion_jobs WHERE user_id = :u",
            "DELETE FROM document_chunks WHERE user_id = :u",
            "DELETE FROM documents WHERE user_id = :u",
            "DELETE FROM users WHERE id = :u",
        ):
            await session.execute(text(stmt), {"u": USER})


def _text(size: int) -> bytes:
    line = b"The Atlas project uses Postgres for storage.\n"
    return (line * (size // len(line) + 1))[:size]


def _multipart(payload: bytes, filename: str = "notes.txt") -> tuple[bytes, str]:
    boundary = "phase11boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


async def _post(client: httpx.AsyncClient, body, content_type: str) -> httpx.Response:  # noqa: ANN001
    return await client.post(
        "/documents", content=body, headers={"Content-Type": content_type, "X-User-Id": USER}
    )


async def test_valid_file_is_accepted(client: httpx.AsyncClient) -> None:
    body, ct = _multipart(_text(10_000))
    r = await _post(client, body, ct)
    assert r.status_code == 201, r.text


async def test_file_exactly_at_the_limit_is_accepted(client: httpx.AsyncClient) -> None:
    body, ct = _multipart(_text(LIMIT))
    r = await _post(client, body, ct)
    assert r.status_code == 201, r.text


async def test_file_one_byte_over_the_limit_is_rejected(client: httpx.AsyncClient) -> None:
    body, ct = _multipart(_text(LIMIT + 1))
    r = await _post(client, body, ct)
    assert r.status_code == 422
    assert r.json()["error"]["details"]["error_code"] == "file_too_large"


async def test_declared_oversize_body_is_refused_before_reading(client: httpx.AsyncClient) -> None:
    body, ct = _multipart(_text(LIMIT * 4))
    r = await _post(client, body, ct)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


async def test_streamed_oversize_body_without_length_is_cut_off(client: httpx.AsyncClient) -> None:
    head, ct = _multipart(b"")
    sent = {"bytes": 0}

    async def stream() -> AsyncIterator[bytes]:
        yield head[:120]
        chunk = _text(16 * 1024)
        for _ in range(64):  # 1 MiB, far past the limit
            sent["bytes"] += len(chunk)
            yield chunk

    r = await _post(client, stream(), ct)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


async def test_malformed_content_is_rejected_cleanly(client: httpx.AsyncClient) -> None:
    body, ct = _multipart(b"\x00\x01\x02binary\xff\xfe" * 100, filename="blob.bin")
    r = await _post(client, body, ct)
    assert r.status_code == 422
    assert r.json()["error"]["details"]["error_code"] == "unsupported_mime"


async def test_truncated_multipart_fails_without_a_server_error(client: httpx.AsyncClient) -> None:
    body, ct = _multipart(_text(5_000))
    r = await _post(client, body[: len(body) // 2], ct)  # no closing boundary
    assert 400 <= r.status_code < 500, r.text


async def test_oversized_upload_does_not_allocate_its_size(client: httpx.AsyncClient) -> None:
    size = 200 * 1024 * 1024  # 200 MiB declared and streamed
    head, ct = _multipart(b"")
    chunk = b"x" * (256 * 1024)

    async def stream() -> AsyncIterator[bytes]:
        yield head[:120]
        for _ in range(size // len(chunk)):
            yield chunk

    tracemalloc.start()
    try:
        r = await client.post(
            "/documents",
            content=stream(),
            headers={"Content-Type": ct, "X-User-Id": USER},
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.status_code == 413
    assert peak < 16 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB for a 200 MiB upload"
