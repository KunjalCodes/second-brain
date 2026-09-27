"""The conversation + live-extraction-trigger HTTP endpoints end to end
(docs/adr/0008), through `TestClient` against real (test) Postgres and real
(test) Neo4j.

This is the regression suite for the FastAPI `BackgroundTasks`/DB-session
race fix in `app/api/routes/conversations.py::append_message` -- the
conversation-path counterpart to the identical fix already made and tested
for `app/api/routes/documents.py::upload_document`
(`tests/integration/test_documents_api.py`). Same underlying cause: FastAPI
runs a `yield`-dependency's exit code (where `get_db_session`'s
`async with db.session()` commits) AFTER `BackgroundTasks`, not before --
so, without the fix, the live background extraction tick could query for a
claimable `ExtractionJob` before this request's own insert of it was ever
committed, and silently find nothing.

`TestClient`'s ASGI transport runs `BackgroundTasks` synchronously as part
of the request/response cycle, so the background tick has already run by
the time `client.post(...)` returns -- no polling needed. The process-wide
`LLM_PROVIDER=fake` singleton (`app.llm.factory.build_llm_client`) is the
offline client (Phase 11), whose extraction completes with zero candidates --
`attempts >= 1` is the proof the tick actually claimed and ran the job, i.e.
that it observed the row this request just committed. A second,
separately-scripted `run_worker_tick` call is used where a test needs to see
the real extraction outcome.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.embeddings.fake import FakeEmbeddingClient
from app.extraction.runner import run_worker_tick
from app.llm.fake import FakeLLMClient
from app.main import app
from app.models.extraction import (
    CandidateEntity,
    CandidateMemory,
    CandidateRelationship,
    DecisionType,
    ExtractionResult,
    Span,
)
from app.repositories.extraction_repository import (
    ExtractionCandidateRepository,
    ExtractionJobRepository,
)

pytestmark = pytest.mark.integration

USER = "itest-conversations-api"
OTHER_USER = "itest-conversations-api-other"
QUALIFYING_CONTENT = "I am building a project called Drawing Extractor that uses PaddleOCR."


@pytest.fixture
def client(db, graph) -> TestClient:  # noqa: ANN001
    """Depends on `db`/`graph` (tests/integration/conftest.py) purely for
    their skip-if-unavailable probing -- the app talks to the same test
    databases through its own process-wide singletons.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
async def _cleanup(db, graph):  # noqa: ANN001, ANN201
    yield
    # FK-safe order (mirrors tests/integration/conftest.py's own
    # `_PG_CLEANUP_STATEMENTS`): `memory_embeddings` must go before `users`
    # -- a scripted extraction in this file's own tests creates a real
    # Memory + embedding, unlike test_documents_api.py's (whose LLM call
    # always fails), so this file needs the fuller list or the `users`
    # delete raises a FK violation that rolls back every delete in the same
    # transaction, silently leaking rows into the next test.
    async with db.session() as session:
        for uid in (USER, OTHER_USER):
            for statement in (
                "DELETE FROM review_audit_log WHERE user_id = :user_id",
                "DELETE FROM memory_review_candidates WHERE user_id = :user_id",
                "DELETE FROM extraction_candidates WHERE user_id = :user_id",
                "DELETE FROM extraction_jobs WHERE user_id = :user_id",
                "DELETE FROM entity_merge_candidates WHERE user_id = :user_id",
                "DELETE FROM memory_embeddings WHERE user_id = :user_id",
                "DELETE FROM messages WHERE user_id = :user_id",
                "DELETE FROM conversations WHERE user_id = :user_id",
                "DELETE FROM users WHERE id = :user_id",
            ):
                await session.execute(text(statement), {"user_id": uid})
        for uid in (USER, OTHER_USER):
            await graph.write("MATCH (n {user_id: $user_id}) DETACH DELETE n", {"user_id": uid})


def _create_conversation(client: TestClient, *, user: str = USER) -> str:
    response = client.post("/conversations", json={"title": "t"}, headers={"X-User-Id": user})
    assert response.status_code == 201, response.text
    return response.json()["id"]


class TestAppendMessageTriggersExtraction:
    async def test_qualifying_message_creates_an_extraction_job(
        self, client: TestClient, db
    ) -> None:
        """Requirement 1: appending a message that passes the extraction
        gate creates the `ExtractionJob` row, and the response already
        reflects `extraction_status="queued"` -- both synchronous, no
        background task involved yet.
        """
        conversation_id = _create_conversation(client)
        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"role": "user", "content": QUALIFYING_CONTENT},
            headers={"X-User-Id": USER},
        )
        assert response.status_code == 201, response.text
        assert response.json()["extraction_status"] == "queued"

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=USER).list_recent(
                limit=5, offset=0
            )
        assert len(jobs) == 1
        assert jobs[0].conversation_id == conversation_id

    async def test_background_tick_observes_the_committed_job(self, client: TestClient, db) -> None:
        """Requirement 2 -- the actual race-fix regression test. Without the
        fix, the job row would not yet be committed when the background
        tick's own `claim_batch` query ran, so it would find nothing and the
        job would still be `status="pending"`, `attempts=0`. With the fix,
        the tick observes and claims it -- `attempts` is incremented as part
        of the same `UPDATE ... SET attempts = attempts + 1` that claims a
        job (`ExtractionQueue.claim_batch`), so `attempts >= 1` is proof the
        background tick actually saw the row this request committed, not
        merely that the row exists.
        """
        conversation_id = _create_conversation(client)
        client.post(
            f"/conversations/{conversation_id}/messages",
            json={"role": "user", "content": QUALIFYING_CONTENT},
            headers={"X-User-Id": USER},
        )

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=USER).list_recent(
                limit=5, offset=0
            )
        assert len(jobs) == 1
        job = jobs[0]
        assert job.attempts >= 1, "the background tick never claimed the job -- the race is back"
        assert job.status.value != "pending", "a claimed-and-processed job is never left pending"

    async def test_extraction_decisions_are_unchanged_for_an_api_created_job(
        self, client: TestClient, db, graph
    ) -> None:
        """Requirement 3: conversation extraction's actual decision-engine
        behavior is unaffected by the fix. The live tick's own attempt fails
        (unscripted global FakeLLM, expected -- see module docstring), which
        leaves the job retryable; processing it again with a properly
        scripted `FakeLLMClient`, exactly like `test_extraction_runner.py`
        does, must still reach the same `NEW` decision passive extraction
        has always reached for this exact scenario.
        """
        # Phase 11: the app's `LLM_PROVIDER=fake` client is now the offline
        # client, whose extraction SUCCEEDS with zero candidates. This test's
        # scenario needs the live tick's attempt to FAIL (leaving the job
        # retryable), so script that failure explicitly for this request.
        from app.api.deps import get_llm
        from app.core.errors import LLMUnavailableError

        client.app.dependency_overrides[get_llm] = lambda: FakeLLMClient(
            failure=LLMUnavailableError("scripted live-tick failure")
        )
        try:
            conversation_id = _create_conversation(client)
            response = client.post(
                f"/conversations/{conversation_id}/messages",
                json={"role": "user", "content": QUALIFYING_CONTENT},
                headers={"X-User-Id": USER},
            )
        finally:
            client.app.dependency_overrides.pop(get_llm, None)
        message_id = response.json()["id"]

        candidate = CandidateMemory(
            statement="User is building a project called Drawing Extractor that uses PaddleOCR",
            memory_type="semantic",
            persistence="persistent",
            confidence=0.9,
            entities=[
                CandidateEntity(name="Drawing Extractor", entity_type="project"),
                CandidateEntity(name="PaddleOCR", entity_type="technology"),
            ],
            relationships=[
                CandidateRelationship(
                    source="Drawing Extractor", predicate="uses", target="PaddleOCR"
                )
            ],
            source_message_id=message_id,
            source_span=Span(start=0, end=len(QUALIFYING_CONTENT)),
            reasoning="regression fixture",
        )
        llm = FakeLLMClient(response=ExtractionResult(candidates=[candidate]))
        from datetime import UTC, datetime, timedelta

        from app.core.config import get_settings

        settings = get_settings()
        # The live tick's own failed attempt (module docstring) left the job
        # retryable but backed off ~60s into the future
        # (`app.extraction.runner._retry_backoff`) -- reclaiming it
        # immediately, as a real retry eventually would, requires a `now_fn`
        # past that backoff window rather than the real current time.
        future = datetime.now(UTC) + timedelta(minutes=5)
        await run_worker_tick(
            db=db,
            graph_client=graph,
            llm_client=llm,
            embedding_client=FakeEmbeddingClient(),
            settings=settings,
            worker_id="itest-conversations-api-retry",
            limit=5,
            now_fn=lambda: future,
        )

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=USER).list_recent(
                limit=5, offset=0
            )
            records = await ExtractionCandidateRepository(session, user_id=USER).list_for_job(
                extraction_job_id=jobs[0].id
            )
        assert [r.decision for r in records] == [DecisionType.NEW]

    async def test_exactly_one_job_is_created_not_duplicated_by_the_commit_fix(
        self, client: TestClient, db
    ) -> None:
        """Requirement 5 (idempotency): the explicit early `db_session.commit()`
        added by the fix, followed by `get_db_session`'s own commit-on-exit
        (now a no-op, nothing left pending), must not cause the job -- or
        anything else `service.append_message` wrote -- to be committed or
        recorded twice.
        """
        conversation_id = _create_conversation(client)
        client.post(
            f"/conversations/{conversation_id}/messages",
            json={"role": "user", "content": QUALIFYING_CONTENT},
            headers={"X-User-Id": USER},
        )

        async with db.session() as session:
            jobs = await ExtractionJobRepository(session, user_id=USER).list_recent(
                limit=10, offset=0
            )
        assert len(jobs) == 1, f"expected exactly one job, got {len(jobs)}"


class TestCrossUserIsolation:
    async def test_another_users_conversation_and_messages_are_not_visible(
        self, client: TestClient, db
    ) -> None:
        """Requirement 4: cross-user isolation is unaffected by the fix --
        neither the conversation nor its extraction job leak to another user.
        """
        conversation_id = _create_conversation(client, user=USER)
        client.post(
            f"/conversations/{conversation_id}/messages",
            json={"role": "user", "content": QUALIFYING_CONTENT},
            headers={"X-User-Id": USER},
        )

        response = client.get(
            f"/conversations/{conversation_id}", headers={"X-User-Id": OTHER_USER}
        )
        assert response.status_code == 404

        async with db.session() as session:
            other_jobs = await ExtractionJobRepository(session, user_id=OTHER_USER).list_recent(
                limit=10, offset=0
            )
        assert other_jobs == []
