"""The document HTTP endpoints end to end (Phase 6, PHASE6_PLAN.md section 3),
through `TestClient` against the real (test) Postgres, real (test) Neo4j, and
local-filesystem storage (`DOCUMENT_STORAGE_DIR`, redirected to `tmp_path` by
`tests/conftest.py`'s `_test_environment` fixture) -- the one place that
proves the dependency wiring in `app/api/deps.py` and `app/main.py` (not
just the service layer directly, which `test_document_ingestion_pipeline.py`
already covers) is correct.

`TestClient`'s ASGI transport runs `BackgroundTasks` synchronously as part of
the request/response cycle, so the upload's background ingest-then-extract
tick has already completed by the time `client.post(...)` returns -- no
polling needed.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration

USER = "itest-documents-api"


@pytest.fixture
def client(db, graph) -> TestClient:  # noqa: ANN001
    """Depends on `db`/`graph` (from tests/integration/conftest.py) purely
    for their skip-if-unavailable probing -- the app itself talks to the
    same test databases through its own process-wide singletons, configured
    by the same environment `tests/conftest.py` already sets up.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
async def _cleanup(db):  # noqa: ANN001, ANN201
    """No Neo4j cleanup needed here: the process-wide `LLM_PROVIDER=fake`
    singleton is constructed with no scripted response
    (`app.llm.factory.build_llm_client`), so every background extraction
    tick's LLM call fails (caught, logged, never raised to the client) and
    no Memory node is ever created by this file's tests. Only the
    PostgreSQL rows these tests do create need cleaning up.
    """
    yield
    from sqlalchemy import text

    async with db.session() as session:
        for uid in (USER, "itest-someone-else"):
            for statement in (
                "DELETE FROM extraction_candidates WHERE user_id = :user_id",
                "DELETE FROM extraction_jobs WHERE user_id = :user_id",
                "DELETE FROM ingestion_jobs WHERE user_id = :user_id",
                "DELETE FROM document_chunks WHERE user_id = :user_id",
                "DELETE FROM documents WHERE user_id = :user_id",
                "DELETE FROM users WHERE id = :user_id",
            ):
                await session.execute(text(statement), {"user_id": uid})


class TestUploadAndInspect:
    def test_upload_ingests_extracts_and_is_listable(self, client: TestClient) -> None:
        content = b"Drawing Extractor is a project that uses PaddleOCR."
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", io.BytesIO(content), "text/plain")},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["filename"] == "notes.txt"
        assert "storage_key" not in body
        document_id = body["id"]

        get_response = client.get(f"/documents/{document_id}", headers={"X-User-Id": USER})
        assert get_response.status_code == 200
        # The background ingest tick already ran synchronously (TestClient's
        # ASGI transport awaits BackgroundTasks before returning) -- the
        # document has moved out of UPLOADED.
        assert get_response.json()["status"] != "uploaded"

        list_response = client.get("/documents", headers={"X-User-Id": USER})
        assert list_response.status_code == 200
        assert any(d["id"] == document_id for d in list_response.json()["documents"])

        chunks_response = client.get(
            f"/documents/{document_id}/chunks", headers={"X-User-Id": USER}
        )
        assert chunks_response.status_code == 200
        chunks = chunks_response.json()["chunks"]
        assert len(chunks) == 1
        assert chunks[0]["content"] == content.decode()

    def test_uploading_the_same_bytes_twice_does_not_duplicate(self, client: TestClient) -> None:
        content = b"Idempotency check content."
        first = client.post(
            "/documents",
            files={"file": ("a.txt", io.BytesIO(content), "text/plain")},
            headers={"X-User-Id": USER},
        )
        second = client.post(
            "/documents",
            files={"file": ("b.txt", io.BytesIO(content), "text/plain")},
            headers={"X-User-Id": USER},
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

    def test_empty_file_is_rejected_with_422(self, client: TestClient) -> None:
        response = client.post(
            "/documents",
            files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_unknown_document_is_404(self, client: TestClient) -> None:
        response = client.get(
            "/documents/00000000-0000-0000-0000-000000000000", headers={"X-User-Id": USER}
        )
        assert response.status_code == 404

    def test_another_users_document_is_not_visible(self, client: TestClient) -> None:
        content = b"Private document content."
        upload = client.post(
            "/documents",
            files={"file": ("private.txt", io.BytesIO(content), "text/plain")},
            headers={"X-User-Id": USER},
        )
        document_id = upload.json()["id"]

        response = client.get(
            f"/documents/{document_id}", headers={"X-User-Id": "itest-someone-else"}
        )
        assert response.status_code == 404
