"""Phase 11 (D5): with no trustworthy client IP, limits are explicitly global.

`RATE_LIMIT_PER_IP=false` (staging behind the Next.js proxy / a platform load
balancer): the login ceiling is ONE service-wide bucket with its own, higher
limit -- not a per-IP bucket that silently becomes shared -- and no per-IP
counter is ever written. The per-email limit still applies.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from tests.integration.test_auth import PASSWORD, Clock, Harness, _build_app, _cleanup, _session_env

pytestmark = pytest.mark.integration


@pytest.fixture
async def g(monkeypatch, db, graph) -> AsyncIterator[Harness]:  # noqa: ANN001
    _session_env(
        monkeypatch,
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_PER_IP="false",
        RATE_LIMIT_LOGIN_GLOBAL="4",
        RATE_LIMIT_LOGIN_PER_EMAIL="2",
    )
    harness = Harness(app=None, db=db, graph=graph, clock=Clock())
    harness.app = _build_app(harness)
    yield harness
    await _cleanup(harness)
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM rate_limit_counters WHERE bucket_key LIKE 'auth_login_global:%'")
        )


async def test_login_ceiling_is_global_and_no_ip_bucket_exists(g: Harness) -> None:
    emails = [(await g.account())[1] for _ in range(3)]
    statuses = []
    async with g.client() as c:
        for i in range(5):
            email = emails[i % 3]
            r = await c.post(
                "/auth/login",
                json={"email": email, "password": "wrong password " + uuid.uuid4().hex[:4]},
                headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},  # distinct "IPs"
            )
            statuses.append(
                (r.status_code, r.json()["error"].get("details", {}).get("limit_class"))
            )
    # 4 attempts allowed service-wide, whatever the claimed client IP; the 5th is refused.
    assert [s for s, _ in statuses[:4]] == [401, 401, 401, 401]
    assert statuses[4] == (429, "auth_login_global")
    async with g.db.session() as session:
        ip_rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM rate_limit_counters "
                    "WHERE bucket_key LIKE 'auth_login_ip:%203.0.113.%'"
                )
            )
        ).scalar()
    assert ip_rows == 0, "no per-IP identity is pretended"


async def test_per_email_limit_still_applies(g: Harness) -> None:
    _, email = await g.account()
    async with g.client() as c:
        codes = [
            (
                await c.post("/auth/login", json={"email": email, "password": "wrong password!!"})
            ).status_code
            for _ in range(3)
        ]
        ok = await c.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert codes == [401, 401, 429]
    assert ok.status_code == 429  # the email bucket is exhausted for this window
