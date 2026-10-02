"""Phase 16 step 1: projects as isolated memory spaces, and scoped project
tokens -- real HTTP, real PostgreSQL, real Neo4j, SESSION mode.

Every account, project, token, space row and graph node is removed afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.integration.test_auth import (
    Clock,
    Harness,
    _build_app,
    _cleanup,
    _memory,
    _session_env,
)

pytestmark = pytest.mark.integration

REPO_A = "https://github.com/acme/auth-service"
REPO_B = "https://github.com/acme/billing"


@pytest.fixture
async def h(monkeypatch, db, graph) -> AsyncIterator[Harness]:  # noqa: ANN001
    _session_env(monkeypatch)
    harness = Harness(app=None, db=db, graph=graph, clock=Clock())
    harness.app = _build_app(harness)
    yield harness
    # Projects first (RESTRICT FK), then adopt their space ids so the shared
    # cleanup also removes the data and `users` rows those spaces own.
    async with db.session() as session:
        spaces: list[str] = []
        for uid in harness.user_ids:
            rows = await session.execute(
                text("SELECT space_id FROM projects WHERE owner_user_id = :u"), {"u": uid}
            )
            spaces += [r[0] for r in rows]
            await session.execute(
                text(
                    "DELETE FROM project_tokens WHERE project_id IN "
                    "(SELECT id FROM projects WHERE owner_user_id = :u)"
                ),
                {"u": uid},
            )
            await session.execute(text("DELETE FROM projects WHERE owner_user_id = :u"), {"u": uid})
    harness.user_ids.extend(spaces)
    await _cleanup(harness)


async def _register(c: AsyncClient, headers: dict, url: str = REPO_A, **kw) -> dict:  # noqa: ANN003
    r = await c.post(
        "/projects", json={"name": "Auth", "repository_url": url, **kw}, headers=headers
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _token(c: AsyncClient, headers: dict, project_id: str, scopes=("read",), **kw) -> dict:  # noqa: ANN001, ANN003
    r = await c.post(
        f"/projects/{project_id}/tokens",
        json={"label": "mcp", "scopes": list(scopes), **kw},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# -- Registration and identity ---------------------------------------------------------------


class TestRegistration:
    async def test_register_is_idempotent_across_url_spellings(self, h: Harness) -> None:
        async with h.client() as c:
            _, hd = await h.user(c)
            r1 = await c.post(
                "/projects", json={"name": "Auth", "repository_url": REPO_A}, headers=hd
            )
            r2 = await c.post(
                "/projects",
                json={
                    "name": "other name",
                    "repository_url": "git@GitHub.com:Acme/Auth-Service.git",
                },
                headers=hd,
            )
        assert (r1.status_code, r2.status_code) == (201, 200)
        assert r1.json()["created"] is True and r2.json()["created"] is False
        assert r1.json()["project"]["id"] == r2.json()["project"]["id"]
        assert r2.json()["project"]["name"] == "Auth"  # the first registration wins

    async def test_response_never_exposes_the_space_id_or_a_path(self, h: Harness) -> None:
        async with h.client() as c:
            _, hd = await h.user(c)
            body = await _register(c, hd)
        raw = str(body)
        assert "space" not in raw and "proj_" not in raw and "owner" not in raw

    async def test_a_filesystem_path_is_refused_and_nothing_is_created(self, h: Harness) -> None:
        async with h.client() as c:
            uid, hd = await h.user(c)
            for bad in ("/Users/me/code/repo", "~/repo", "file:///x/y", "C:\\code\\repo"):
                r = await c.post("/projects", json={"name": "x", "repository_url": bad}, headers=hd)
                assert r.status_code == 422, (bad, r.text)
            r = await c.post("/projects", json={"name": "x"}, headers=hd)
            assert r.status_code == 422
            listed = await c.get("/projects", headers=hd)
        assert listed.json()["items"] == []
        async with h.db.session() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM projects WHERE owner_user_id=:u"), {"u": uid}
                )
            ).scalar_one()
        assert n == 0

    async def test_root_commit_identity_for_a_repo_without_a_remote(self, h: Harness) -> None:
        async with h.client() as c:
            _, hd = await h.user(c)
            payload = {"name": "local", "root_commits": ["a" * 40]}
            r1 = await c.post("/projects", json=payload, headers=hd)
            r2 = await c.post("/projects", json={**payload, "root_commits": ["A" * 40]}, headers=hd)
        assert r1.json()["project"]["identity_kind"] == "root_commits"
        assert r1.json()["project"]["id"] == r2.json()["project"]["id"]

    @pytest.mark.parametrize("field", ["user_id", "space_id", "owner_user_id", "id", "project_id"])
    async def test_authority_fields_in_a_body_are_a_422(self, h: Harness, field: str) -> None:
        async with h.client() as c:
            _, hd = await h.user(c)
            r = await c.post(
                "/projects",
                json={"name": "x", "repository_url": REPO_A, field: "someone-else"},
                headers=hd,
            )
        assert r.status_code == 422

    async def test_the_same_repository_for_two_accounts_is_two_projects(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            _, hb = await h.user(c)
            a, b = await _register(c, ha), await _register(c, hb)
        assert a["created"] and b["created"] and a["project"]["id"] != b["project"]["id"]

    async def test_concurrent_registration_creates_exactly_one_project(self, h: Harness) -> None:
        async with h.client() as c:
            uid, hd = await h.user(c)
            results = await asyncio.gather(*[_register(c, hd) for _ in range(6)])
        assert sum(r["created"] for r in results) == 1
        assert len({r["project"]["id"] for r in results}) == 1
        async with h.db.session() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM projects WHERE owner_user_id=:u"), {"u": uid}
                )
            ).scalar_one()
            spaces = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM users WHERE id IN "
                        "(SELECT space_id FROM projects WHERE owner_user_id=:u)"
                    ),
                    {"u": uid},
                )
            ).scalar_one()
        assert n == 1 and spaces == 1

    async def test_project_limit_per_account(self, h: Harness, monkeypatch) -> None:  # noqa: ANN001
        from app.core.config import get_settings

        monkeypatch.setenv("PROJECTS_MAX_PER_USER", "2")
        get_settings.cache_clear()
        async with h.client() as c:
            _, hd = await h.user(c)
            await _register(c, hd, "https://github.com/a/one")
            await _register(c, hd, "https://github.com/a/two")
            r = await c.post(
                "/projects",
                json={"name": "3", "repository_url": "https://github.com/a/three"},
                headers=hd,
            )
            again = await _register(c, hd, "https://github.com/a/one")  # existing is still fine
        assert r.status_code == 409 and again["created"] is False


# -- Ownership and isolation -----------------------------------------------------------------


class TestIsolation:
    async def test_listing_and_reading_are_per_account(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            _, hb = await h.user(c)
            pa = (await _register(c, ha))["project"]["id"]
            await _register(c, hb, REPO_B)
            la = (await c.get("/projects", headers=ha)).json()["items"]
            foreign = await c.get(f"/projects/{pa}", headers=hb)
            own = await c.get(f"/projects/{pa}", headers=ha)
        assert [p["id"] for p in la] == [pa]
        assert foreign.status_code == 404 and own.status_code == 200

    async def test_a_foreign_project_is_indistinguishable_from_a_missing_one(
        self, h: Harness
    ) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            _, hb = await h.user(c)
            pa = (await _register(c, ha))["project"]["id"]
            foreign = await c.get(f"/projects/{pa}", headers=hb)
            missing = await c.get("/projects/00000000-0000-4000-8000-000000000000", headers=hb)
            garbage = await c.get("/projects/not-a-uuid", headers=hb)
        assert foreign.status_code == missing.status_code == garbage.status_code == 404
        assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]

    async def test_memory_space_header_selects_only_an_owned_project(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            _, hb = await h.user(c)
            pa = (await _register(c, ha))["project"]["id"]
            written = await c.post(
                "/memories", json=_memory("Auth uses Redis."), headers={**ha, "X-Memory-Space": pa}
            )
            in_space = await c.get("/memories/search", headers={**ha, "X-Memory-Space": pa})
            personal = await c.get("/memories/search", headers=ha)
            other_user = await c.get("/memories/search", headers={**hb, "X-Memory-Space": pa})
            garbage = await c.get(
                "/memories/search", headers={**ha, "X-Memory-Space": "'; DROP TABLE users;--"}
            )
        assert written.status_code == 201, written.text
        assert [m["content"] for m in in_space.json()["items"]] == ["Auth uses Redis."]
        assert personal.json()["items"] == []  # memory space and personal space are separate
        assert other_user.status_code == 404 and garbage.status_code == 404

    async def test_two_projects_of_one_account_do_not_see_each_other(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            p1 = (await _register(c, ha, REPO_A))["project"]["id"]
            p2 = (await _register(c, ha, REPO_B))["project"]["id"]
            await c.post(
                "/memories",
                json=_memory("Project one uses Gemini.", "One"),
                headers={**ha, "X-Memory-Space": p1},
            )
            await c.post(
                "/memories",
                json=_memory("Project two uses Redis.", "Two", "Redis"),
                headers={**ha, "X-Memory-Space": p2},
            )
            s1 = await c.get("/memories/search", headers={**ha, "X-Memory-Space": p1})
            s2 = await c.get("/memories/search", headers={**ha, "X-Memory-Space": p2})
            e1 = await c.get(
                "/entities/search", params={"q": "Two"}, headers={**ha, "X-Memory-Space": p1}
            )
        assert [m["content"] for m in s1.json()["items"]] == ["Project one uses Gemini."]
        assert [m["content"] for m in s2.json()["items"]] == ["Project two uses Redis."]
        assert e1.json()["items"] == []

    async def test_auth_routes_are_never_redirected_into_a_space(self, h: Harness) -> None:
        async with h.client() as c:
            uid, ha = await h.user(c)
            pa = (await _register(c, ha))["project"]["id"]
            me = await c.get("/auth/me", headers={**ha, "X-Memory-Space": pa})
        assert me.json()["user"]["id"] == uid


# -- Project tokens --------------------------------------------------------------------------


class TestTokens:
    async def test_secret_is_shown_once_stored_hashed_and_never_listed(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            created = await _token(c, ha, pid, ("read", "ingest"))
            listed = await c.get(f"/projects/{pid}/tokens", headers=ha)
            project = await c.get(f"/projects/{pid}", headers=ha)
        secret = created["secret"]
        assert secret.startswith("sbp_") and len(secret) == 47
        assert secret not in listed.text and secret not in project.text
        assert "secret" not in listed.json()["items"][0]
        async with h.db.session() as s:
            digest = (
                await s.execute(
                    text("SELECT token_sha256 FROM project_tokens WHERE id=:i"),
                    {"i": created["token"]["id"]},
                )
            ).scalar_one()
            stored = (
                await s.execute(text("SELECT string_agg(t::text, ' ') FROM project_tokens t"))
            ).scalar_one()
        assert digest == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in stored

    async def test_a_token_authenticates_only_its_own_project(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            p1 = (await _register(c, ha, REPO_A))["project"]["id"]
            p2 = (await _register(c, ha, REPO_B))["project"]["id"]
            secret = (await _token(c, ha, p1))["secret"]
            own = await c.get(f"/projects/{p1}", headers=_bearer(secret))
            current = await c.get("/projects/current", headers=_bearer(secret))
            sibling = await c.get(f"/projects/{p2}", headers=_bearer(secret))  # same account!
        assert own.status_code == 200 and current.json()["id"] == p1
        assert sibling.status_code == 404

    async def test_a_token_is_not_a_session(self, h: Harness) -> None:
        """It must fail every route that is not `/projects/{id}/...`."""
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            tk = _bearer((await _token(c, ha, pid, ("read", "ingest", "propose")))["secret"])
            statuses = [
                (await c.get("/projects", headers=tk)).status_code,
                (await c.get("/memories/search", headers=tk)).status_code,
                (await c.get("/graph/overview", headers=tk)).status_code,
                (await c.get("/auth/me", headers=tk)).status_code,
                (await c.post("/auth/logout-all", headers=tk)).status_code,
                (
                    await c.get("/memories/search", headers={**tk, "X-Memory-Space": pid})
                ).status_code,
            ]
        assert statuses == [401] * 6

    async def test_a_token_cannot_mint_or_revoke_tokens(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            made = await _token(c, ha, pid)
            tk = _bearer(made["secret"])
            mint = await c.post(
                f"/projects/{pid}/tokens", json={"label": "x", "scopes": ["read"]}, headers=tk
            )
            listing = await c.get(f"/projects/{pid}/tokens", headers=tk)
            revoke = await c.post(
                f"/projects/{pid}/tokens/{made['token']['id']}/revoke", headers=tk
            )
        assert (mint.status_code, listing.status_code, revoke.status_code) == (403, 403, 403)

    async def test_a_session_cannot_be_used_as_a_project_token_and_vice_versa(
        self, h: Harness
    ) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            via_session = await c.get(f"/projects/{pid}", headers=ha)  # owner session: allowed
            current_via_session = await c.get("/projects/current", headers=ha)
        assert via_session.status_code == 200 and current_via_session.status_code == 404

    async def test_revoked_expired_forged_and_malformed_tokens_all_fail_the_same_way(
        self, h: Harness
    ) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            revoked = await _token(c, ha, pid)
            expiring = await _token(c, ha, pid, ttl_days=1)
            live = await _token(c, ha, pid)
            assert (
                await c.get(f"/projects/{pid}", headers=_bearer(revoked["secret"]))
            ).status_code == 200
            r = await c.post(f"/projects/{pid}/tokens/{revoked['token']['id']}/revoke", headers=ha)
            assert r.status_code == 204
            h.clock.advance(days=2)
            outcomes = {
                "revoked": await c.get(f"/projects/{pid}", headers=_bearer(revoked["secret"])),
                "expired": await c.get(f"/projects/{pid}", headers=_bearer(expiring["secret"])),
                "forged": await c.get(f"/projects/{pid}", headers=_bearer("sbp_" + "A" * 43)),
                "malformed": await c.get(f"/projects/{pid}", headers=_bearer("sbp_nope")),
                "empty": await c.get(f"/projects/{pid}", headers={"Authorization": "Bearer "}),
                "live": await c.get(f"/projects/{pid}", headers=_bearer(live["secret"])),
            }
        codes = {k: v.status_code for k, v in outcomes.items()}
        assert codes == {
            **dict.fromkeys(["revoked", "expired", "forged", "malformed", "empty"], 401),
            "live": 200,
        }
        errors = {
            (outcomes[k].json()["error"]["code"], outcomes[k].json()["error"]["message"])
            for k in ("revoked", "expired", "forged", "malformed")
        }
        assert len(errors) == 1  # one generic error: no oracle for the cause

    async def test_token_limit_per_project(self, h: Harness, monkeypatch) -> None:  # noqa: ANN001
        from app.core.config import get_settings

        monkeypatch.setenv("PROJECT_TOKENS_MAX_PER_PROJECT", "2")
        get_settings.cache_clear()
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            first = await _token(c, ha, pid)
            await _token(c, ha, pid)
            third = await c.post(
                f"/projects/{pid}/tokens", json={"label": "3", "scopes": ["read"]}, headers=ha
            )
            await c.post(f"/projects/{pid}/tokens/{first['token']['id']}/revoke", headers=ha)
            after = await c.post(
                f"/projects/{pid}/tokens", json={"label": "4", "scopes": ["read"]}, headers=ha
            )
        assert third.status_code == 409 and after.status_code == 201

    async def test_token_requests_are_validated(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            bad = [
                {"label": "x", "scopes": []},
                {"label": "x", "scopes": ["admin"]},
                {"label": "", "scopes": ["read"]},
                {"label": "x", "scopes": ["read"], "ttl_days": 9999},
                {"label": "x", "scopes": ["read"], "user_id": "someone"},
                {"label": "x", "scopes": ["read"], "project_id": pid},
            ]
            codes = [
                (await c.post(f"/projects/{pid}/tokens", json=b, headers=ha)).status_code
                for b in bad
            ]
        assert codes == [422] * len(bad)

    async def test_a_foreign_account_cannot_touch_my_tokens(self, h: Harness) -> None:
        async with h.client() as c:
            _, ha = await h.user(c)
            _, hb = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            made = await _token(c, ha, pid)
            calls = [
                await c.post(
                    f"/projects/{pid}/tokens", json={"label": "x", "scopes": ["read"]}, headers=hb
                ),
                await c.get(f"/projects/{pid}/tokens", headers=hb),
                await c.post(f"/projects/{pid}/tokens/{made['token']['id']}/revoke", headers=hb),
            ]
            still_ok = await c.get(f"/projects/{pid}", headers=_bearer(made["secret"]))
        assert [r.status_code for r in calls] == [404, 404, 404]
        assert still_ok.status_code == 200

    async def test_a_disabled_owner_disables_the_tokens(self, h: Harness) -> None:
        async with h.client() as c:
            uid, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            secret = (await _token(c, ha, pid))["secret"]
            async with h.db.session() as s:
                await s.execute(
                    text("UPDATE user_accounts SET status='disabled' WHERE user_id=:u"), {"u": uid}
                )
            r = await c.get(f"/projects/{pid}", headers=_bearer(secret))
        assert r.status_code == 401

    async def test_the_secret_never_reaches_the_logs(self, h: Harness, caplog) -> None:  # noqa: ANN001
        caplog.set_level(logging.DEBUG)
        async with h.client() as c:
            _, ha = await h.user(c)
            pid = (await _register(c, ha))["project"]["id"]
            secret = (await _token(c, ha, pid))["secret"]
            await c.get(f"/projects/{pid}", headers=_bearer(secret))
            await c.get(f"/projects/{pid}", headers=_bearer(secret[:-1] + "x"))
        assert secret not in caplog.text and secret[:-1] not in caplog.text
