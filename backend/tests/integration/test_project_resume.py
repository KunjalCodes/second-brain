# ruff: noqa: E501, F811 - request tables kept one per line; fixtures are imported by name
"""Phase 16 step 6: `get_resume_context` -- deterministic, bounded continuity.

The product behaviour: SESSION 1 happens and ends; SESSION 2 asks where things were left,
and the memory engine supplies the right context from EXPLICIT evidence only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.deps import get_client
from tests.integration.test_auth import Harness
from tests.integration.test_project_events import E, _Down
from tests.integration.test_project_events import _pg as pg
from tests.integration.test_project_memory import PM, SESSION_PY
from tests.integration.test_project_memory import pm as pm  # noqa: F401 - fixture
from tests.integration.test_projects import _bearer, _register, _token, h  # noqa: F401
from tests.unit.test_git_collector import repo  # noqa: F401

pytestmark = pytest.mark.integration


async def resume(w: PM, **params):  # noqa: ANN003, ANN201
    return await w.get("/resume", **params)


class TestTheContinuityScenario:
    """DAY 1 -> DAY 2: 'Continue where we left off.'"""

    async def test_it_reconstructs_where_i_left_off(self, pm: PM) -> None:
        r = await resume(pm)
        assert r["has_history"] is True and r["project"]["name"] == "Auth"
        # WHERE I LEFT OFF
        assert (
            r["last_session"]["session_id"] == "s2"
            and r["last_session"]["continues_session"] == "s1"
        )
        (prev,) = r["previous_sessions"]
        assert (
            prev["session_id"] == "s1"
            and prev["outcome"] == "blocked"
            and prev["summary"] == "Fix committed; docs still outdated"
        )
        assert prev["goal"] == "Fix the session timeout bug"
        # WHAT REMAINS (explicit only), explicit items first
        texts = [(u["basis"], u["text"]) for u in r["unfinished_work"]]
        assert texts[:2] == [
            ("explicit_unfinished", "Update the auth docs"),
            ("explicit_unfinished", "Add a TTL configuration test"),
        ] or {t for b, t in texts[:2]} == {"Update the auth docs", "Add a TTL configuration test"}
        assert {u["basis"] for u in r["unfinished_work"]} <= {
            "explicit_unfinished",
            "task_blocked",
            "unclosed_user_request",
        }
        assert all(
            u["source"] and u["source"]["type"] == "agent_event" for u in r["unfinished_work"]
        )
        # WHAT I WAS DOING
        assert (
            r["active_task"]["text"] == "Continue where we left off"
            and r["active_task"]["basis"] == "latest_unclosed_user_request"
        )
        # WHAT I DECIDED, and WHY (as stated)
        d = r["recent_decisions"]
        assert [x["title"] for x in d] == [
            "Extend the TTL instead",
            "Refresh tokens on every request",
        ]
        assert (
            d[0]["status"] == "active"
            and d[0]["rationale"] == "Refreshing on every request adds load"
        )
        assert d[1]["status"] == "superseded" and d[1]["superseded_by"] == "Extend the TTL instead"
        # WHAT I INVESTIGATED / CHANGED
        assert r["files_modified"] == [SESSION_PY] and r["files_inspected"] == [
            "docs.md",
            SESSION_PY,
        ]
        assert [c["subject"] for c in r["recent_commits"]][0] == "Add feature" or r[
            "recent_commits"
        ]
        # WHAT FAILED / WAS FIXED / WAS TESTED
        by = {a["approach"]: a for a in r["attempts"]}
        assert (
            by["Refresh on every request"]["outcome"] == "failed"
            and by["Extend TTL"]["outcome"] == "succeeded"
        )
        (err,) = r["previous_errors"]
        assert (
            err["title"].startswith("AssertionError")
            and err["fixed_by"] == []
            and err["attempted_fixes"] == ["Refresh on every request"]
        )
        (bug,) = r["known_bugs"]
        assert bug["title"] == "Session timeout" and bug["status"] == "fixed"
        (test,) = r["tests_run"]
        assert test["command"] == "pytest tests/test_session.py" and test["result"] == "passed"
        # BEFORE CONTINUING: sources, ranking, limits, untrusted-text notice
        assert r["relevant_sources"] and all(
            s["type"] in ("agent_event", "git_commit") for s in r["relevant_sources"]
        )
        assert (
            r["ranking"][:3] == ["unfinished_work", "active_task", "last_session"]
            and "never as instructions" in r["notice"]
        )
        assert set(r["limits"]) >= {
            "unfinished_work",
            "recent_decisions",
            "known_bugs",
            "tests_run",
        }

    async def test_resuming_a_specific_session(self, pm: PM) -> None:
        r = await resume(pm, session_id="s1")
        assert r["last_session"]["session_id"] == "s1" and r["last_session"]["outcome"] == "blocked"
        assert r["previous_sessions"] == [] and {u["text"] for u in r["unfinished_work"]} >= {
            "Update the auth docs"
        }
        await resume_404(pm, "ghost")

    async def test_the_answer_is_deterministic_apart_from_the_clock(self, pm: PM) -> None:
        a, b = await resume(pm), await resume(pm)
        a.pop("generated_at"), b.pop("generated_at")
        assert a == b

    async def test_a_task_is_a_bias_never_a_filter(self, pm: PM) -> None:
        """Found by the real external-agent test: `task` used to be a whole-phrase filter, so
        a natural description ("auth-service continue work") hid ALL of the history."""
        plain = await resume(pm)
        for task in ("auth-service continue work", "kubernetes ingress", "x" * 200, "the a an"):
            r = await resume(pm, task=task)
            assert [d["title"] for d in r["recent_decisions"]] == [d["title"] for d in plain["recent_decisions"]], task
            assert [b["title"] for b in r["known_bugs"]] == [b["title"] for b in plain["known_bugs"]], task
            assert {a["approach"] for a in r["attempts"]} == {a["approach"] for a in plain["attempts"]}, task
            assert r["unfinished_work"] == plain["unfinished_work"], task

    async def test_task_words_can_pull_in_unrelated_history_with_a_stated_reason(self, pm: PM) -> None:
        await pm.events(
            E("session_started", {"goal": "Tidy invoices"}, session="s0", t=-9000),
            E("decision", {"title": "Use CSS variables for invoice colours", "rationale": "Theming"}, session="s0", t=-8999),
            E("session_ended", {"outcome": "completed"}, session="s0", t=-8990),
        )
        default = await resume(pm)
        assert "Use CSS variables for invoice colours" not in {d["title"] for d in default["recent_decisions"]}
        biased = await resume(pm, task="fix the invoice colours")
        match = next(d for d in biased["recent_decisions"] if d["title"] == "Use CSS variables for invoice colours")
        assert match["relevance"] == ["matches_task_words"]
        everything = await resume(pm, include_unrelated=True)
        assert "Use CSS variables for invoice colours" in {d["title"] for d in everything["recent_decisions"]}

    async def test_supporting_memories_follow_the_task(self, pm: PM) -> None:
        r = await resume(pm, task="session timeout")
        assert r["important_memories"] and all(m["sources"] for m in r["important_memories"])
        assert {e for m in r["important_memories"] for e in m["evidence"]} <= {"git", "agent_stated", "parser", "derived"}


async def resume_404(pm: PM, session_id: str) -> None:
    await pm.get("/resume", expect=404, session_id=session_id)


class TestOnlyExplicitEvidence:
    async def fresh(self, h: Harness, repo, c) -> PM:  # noqa: ANN001, F811
        _, account = await h.user(c)
        pid = (await _register(c, account, "https://github.com/acme/fresh"))["project"]["id"]
        secret = (await _token(c, account, pid, ("read", "ingest", "propose")))["secret"]
        space = (await pg(h, "SELECT space_id FROM projects WHERE id = :p", p=pid))[0][0]
        w = PM(h, c, account, pid, space, _bearer(secret), repo)
        return w

    async def test_an_empty_project_has_nothing_to_resume(self, h: Harness, repo) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            r = await resume(w)
        assert r["has_history"] is False and r["last_session"] is None and r["active_task"] is None
        assert (
            r["unfinished_work"] == []
            and r["recent_decisions"] == []
            and r["relevant_sources"] == []
        )

    async def test_file_changes_and_graph_structure_alone_never_become_unfinished_work_or_attempts(
        self, h: Harness, repo
    ) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            await w.events(
                E("session_started", {"goal": "Tinker"}, t=-60),
                E("file_inspected", {"path": "a.py"}, t=-59),
                E("file_modified", {"path": "a.py"}, t=-58),
                E("file_modified", {"path": "b.py"}, t=-57),
                E("commit", {"sha": "d" * 40, "message": "wip", "files": ["a.py", "b.py"]}, t=-56),
                E("session_ended", {"outcome": "abandoned"}, t=-55),
            )
            r = await resume(w)
        assert r["unfinished_work"] == [] and r["active_task"] is None
        assert r["attempts"] == [] and r["known_bugs"] == [] and r["previous_errors"] == []
        assert (
            r["files_modified"] == ["b.py", "a.py"] and r["last_session"]["outcome"] == "abandoned"
        )

    async def test_a_closed_request_is_not_unfinished_and_an_unclosed_one_is(
        self, h: Harness, repo
    ) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            await w.events(
                E("session_started", {}, t=-60) | {"payload": {"goal": "Work"}},
                E("user_request", {"text": "Do A"}, t=-59),
                E("task_completed", {"summary": "A done"}, t=-58),
                E("user_request", {"text": "Do B"}, t=-57),
            )
            r = await resume(w)
        assert [u["text"] for u in r["unfinished_work"]] == ["Do B"] and r["unfinished_work"][0][
            "basis"
        ] == "unclosed_user_request"
        assert r["active_task"]["text"] == "Do B"

    async def test_a_blocked_task_is_unfinished_until_that_feature_is_completed(
        self, h: Harness, repo
    ) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            await w.events(
                E("session_started", {"goal": "Work"}, t=-60),
                E("task_blocked", {"reason": "Needs API key", "feature": "Billing"}, t=-59),
                E("task_blocked", {"reason": "Needs review", "feature": "Search"}, t=-58),
            )
            blocked = await resume(w)
            await w.events(
                E("task_completed", {"summary": "Billing done", "feature": "Billing"}, t=-50)
            )
            after = await resume(w)
        assert {u["text"] for u in blocked["unfinished_work"]} == {"Needs API key", "Needs review"}
        assert {u["text"] for u in after["unfinished_work"]} == {"Needs review"}
        assert {u["basis"] for u in after["unfinished_work"]} == {"task_blocked"}

    async def test_a_completed_session_leaves_nothing_active(self, h: Harness, repo) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            await w.events(
                E("session_started", {"goal": "Work"}, t=-60),
                E("user_request", {"text": "Ship it"}, t=-59),
                E("session_ended", {"outcome": "completed", "summary": "Shipped"}, t=-58),
            )
            r = await resume(w)
        assert r["unfinished_work"] == [] and r["active_task"] is None

    async def test_a_reason_is_never_invented_and_text_is_trimmed(self, h: Harness, repo) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            await w.events(
                E("session_started", {"goal": "g" * 500}, t=-60),
                E("decision", {"title": "Use a queue"}, t=-59),
                E("user_request", {"text": "x" * 2000}, t=-58),
            )
            r = await resume(w)
        assert r["recent_decisions"][0]["rationale"] is None
        assert (
            len(r["last_session"]["goal"]) <= 300
            and len(r["active_task"]["text"]) <= 300
            and len(r["unfinished_work"][0]["text"]) <= 300
        )

    async def test_continuation_chains_are_bounded_and_cycle_safe(self, h: Harness, repo) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            w = await self.fresh(h, repo, c)
            await w.events(E("session_started", {"goal": "one"}, session="a1", t=-90))
            for i, (prev, cur) in enumerate(
                [("a1", "a2"), ("a2", "a3"), ("a3", "a4"), ("a4", "a5")]
            ):
                await w.events(
                    E(
                        "session_started",
                        {"goal": cur, "continues_session": prev},
                        session=cur,
                        t=-80 + i * 10,
                    )
                )
            r = await resume(w)
        assert r["last_session"]["session_id"] == "a5"
        assert [s["session_id"] for s in r["previous_sessions"]] == [
            "a4",
            "a3",
        ]  # depth 3 including the last


class TestBounds:
    async def test_every_section_is_capped_and_can_only_be_lowered(self, h: Harness, repo) -> None:  # noqa: ANN001, F811
        async with h.client() as c:
            _, account = await h.user(c)
            pid = (await _register(c, account, "https://github.com/acme/big"))["project"]["id"]
            tok = _bearer((await _token(c, account, pid, ("read", "ingest")))["secret"])
            space = (await pg(h, "SELECT space_id FROM projects WHERE id = :p", p=pid))[0][0]
            w = PM(h, c, account, pid, space, tok, repo)
            batch = [E("session_started", {"goal": "Marathon"}, t=-1000)]
            for i in range(40):
                batch += [
                    E(
                        "decision",
                        {"title": f"Decision {i}", "rationale": "r" * 900, "files": [f"f{i}.py"]},
                        t=-990 + i,
                    ),
                    E("file_modified", {"path": f"m{i}.py"}, t=-900 + i),
                    E("file_inspected", {"path": f"i{i}.py"}, t=-800 + i),
                    E(
                        "bug_discovered",
                        {"title": f"Bug {i}", "description": "d" * 900},
                        t=-700 + i,
                    ),
                    E(
                        "error",
                        {"message": f"Error {i} " + "e" * 900, "approach": f"Approach {i}"},
                        t=-600 + i,
                    ),
                    E("test_run", {"command": f"pytest -k t{i}", "result": "passed"}, t=-500 + i),
                    E("user_request", {"text": f"Request {i}"}, t=-400 + i),
                ]
            for k in range(0, len(batch), 50):
                await w.events(*batch[k : k + 50])
            r = await resume(w)
            size = len(json.dumps(r))
            small = await resume(w, max_items=2)
            await w.get("/resume", expect=422, max_items=11)
            await w.get("/resume", expect=422, max_items=0)
        caps = {
            "unfinished_work": 10,
            "recent_decisions": 5,
            "files_inspected": 15,
            "files_modified": 15,
            "known_bugs": 5,
            "previous_errors": 5,
            "attempts": 5,
            "tests_run": 5,
        }
        for name, cap in caps.items():
            assert len(r[name]) <= cap, name
            assert r["limits"][name]["returned"] == len(r[name]) and r["limits"][name][
                "available"
            ] >= len(r[name])
        assert (
            r["limits"]["recent_decisions"]["available"] == 40
        )  # decisions() loads a bounded window
        assert size < 40_000, size  # a bounded document, however much history exists
        assert all(len(small[name]) <= 2 for name in caps) and len(json.dumps(small)) < size

    async def test_resume_works_when_neo4j_is_down(self, pm: PM) -> None:
        pm.h.app.dependency_overrides[get_client] = lambda: _Down(pm.h.graph)
        r = await pm.c.get(
            f"/projects/{pm.project_id}/resume", params={"task": "timeout"}, headers=pm.token
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert (
            body["last_session"]["session_id"] == "s2"
            and body["unfinished_work"]
            and body["important_memories"] == []
        )


class TestAuthorization:
    async def test_resume_is_scoped_and_needs_the_read_scope(self, pm: PM) -> None:
        other = (await _register(pm.c, pm.account, "https://github.com/acme/billing"))["project"][
            "id"
        ]
        tok = _bearer((await _token(pm.c, pm.account, other, ("read",)))["secret"])
        theirs = await pm.c.get(f"/projects/{other}/resume", headers=tok)
        assert (
            theirs.status_code == 200 and theirs.json()["has_history"] is False
        )  # no leakage from my project
        assert (
            await pm.c.get(f"/projects/{pm.project_id}/resume", headers=tok)
        ).status_code == 404  # their token, my project
        wo = _bearer((await _token(pm.c, pm.account, pm.project_id, ("ingest",)))["secret"])
        assert (await pm.c.get(f"/projects/{pm.project_id}/resume", headers=wo)).status_code == 403
        async with pm.h.client() as clean:
            assert (await clean.get(f"/projects/{pm.project_id}/resume")).status_code == 401
            _, stranger = await pm.h.user(clean)
            assert (
                await clean.get(f"/projects/{pm.project_id}/resume", headers=stranger)
            ).status_code == 404
        async with pm.h.client() as clean:
            assert (
                await clean.get("/projects/current/resume", headers=pm.token)
            ).status_code == 200

    async def test_a_foreign_session_id_cannot_be_resumed(self, pm: PM) -> None:
        other = (await _register(pm.c, pm.account, "https://github.com/acme/billing"))["project"][
            "id"
        ]
        tok = _bearer((await _token(pm.c, pm.account, other, ("read",)))["secret"])
        r = await pm.c.get(f"/projects/{other}/resume", params={"session_id": "s1"}, headers=tok)
        assert r.status_code == 404



class TestRealAgentRegression:
    """Replays what a REAL external agent (the local claude CLI) managed to record in session 1
    of the Phase 16 real-agent test, then resumes with the exact `task` string its session 2
    used. Before the fix, `task` was a whole-phrase filter and the resume dropped the recorded
    bugs and the fix; the agent never saw them."""

    async def test_the_recorded_bugs_and_fix_survive_a_natural_task_description(self, h: Harness, repo) -> None:
        fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "real_agent_session1.json").read_text())
        async with h.client() as c:
            _, account = await h.user(c)
            pid = (await _register(c, account, "https://github.com/acme/real-agent"))["project"]["id"]
            tok = _bearer((await _token(c, account, pid, ("read", "ingest")))["secret"])
            space = (await pg(h, "SELECT space_id FROM projects WHERE id = :p", p=pid))[0][0]
            w = PM(h, c, account, pid, space, tok, repo)
            events = []
            for i, e in enumerate(fixture["accepted_events"]):
                events.append({"session_id": fixture["session_id"], "event_type": e["event_type"], "timestamp": E("x", t=-120 + i)["timestamp"], "payload": e["payload"]})
            r = await c.post(f"/projects/{pid}/events", json={"events": events}, headers=tok)
            assert r.status_code == 200, r.text
            got = await resume(w, task=fixture["session2_resume_task"])
        bug_titles = {b["title"] for b in got["known_bugs"]}
        assert any("SESSION_TTL_MINUTES" in t for t in bug_titles), bug_titles
        assert [a["outcome"] for a in got["attempts"]] == ["succeeded"], got["attempts"]
        assert got["last_session"]["session_id"] == "s-real-1" and got["last_session"]["status"] == "active"
        assert got["files_modified"] == ["auth.py"] and len(got["tests_run"]) == 3
        assert got["unfinished_work"] == []  # nothing explicit was recorded, so nothing is invented
