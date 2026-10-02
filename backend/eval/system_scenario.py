# ruff: noqa: E501 - scenario steps are kept one per line
"""Phase 17 (M23): ONE end-to-end scenario against a RUNNING stack.

    integrations/mcp/.venv/bin/python backend/eval/system_scenario.py \
        --engine http://localhost:8001 --out /tmp/scenario.json [--keep-credentials-file creds.json]

Needs only HTTP, the local `git`, the repository's Git collector and the MCP adapter, so it
runs in the adapter's own virtual environment (no backend imports). It uses a SYNTHETIC
account (email `p17-...@example.com`); the matching `eval/system_scenario_cleanup.py`
removes everything it created. No LLM is involved on the engine side, and nothing here
calls a model: the "agent" is a script that plays the part of a coding agent by calling the
real MCP tools over a real stdio MCP connection.

Steps follow docs/PHASE17: project -> scoped credential -> MCP -> session 1 (bug, decision,
change, failing test, error, fix, passing test, commit, unfinished work) -> graph, provenance
and history -> session 2 (a NEW process) asks for resume context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from mcp import StdioServerParameters
from mcp.client import Client

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PY = ROOT / "integrations" / "mcp" / ".venv" / "bin" / "python"
PASSWORD = "correct horse battery staple p17"
RESULTS: list[dict] = []


def step(n: int, name: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append({"step": n, "name": name, "ok": bool(ok), "evidence": evidence})
    print(f"  {'PASS' if ok else 'FAIL'}  {n:>2}. {name}  {'' if ok else evidence}")
    return bool(ok)


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def make_repo(root: Path) -> Path:
    repo = root / "payments-api"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.name", "Scenario Author")
    sh(repo, "git", "config", "user.email", "scenario@example.invalid")
    sh(repo, "git", "remote", "add", "origin", "https://github.com/p17-scenario/payments-api.git")
    (repo / "retry.py").write_text("MAX_RETRIES = 1\n\n\ndef should_retry(attempt: int) -> bool:\n    return attempt < MAX_RETRIES\n")
    (repo / "test_retry.py").write_text("from retry import should_retry\n\n\ndef test_retries_three_times():\n    assert should_retry(2)\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "Initial payments retry logic")
    return repo


def payload(result) -> dict:  # noqa: ANN001
    if result.is_error:
        return {"_error": json.dumps(result.model_dump(mode="json"), default=str)[:300]}
    return result.structured_content or json.loads(result.content[0].text)


def mcp_params(url: str, token: str) -> StdioServerParameters:
    return StdioServerParameters(command=str(ADAPTER_PY), args=["-m", "memory_engine_mcp.server"], env={**os.environ, "MEMORY_ENGINE_URL": url, "MEMORY_ENGINE_TOKEN": token})


async def call(c: Client, tool: str, **args):  # noqa: ANN003, ANN202
    return payload(await c.call_tool(tool, {k: v for k, v in args.items() if v is not None}))


async def main_async(a: argparse.Namespace) -> int:
    url = a.engine.rstrip("/")
    tag = uuid.uuid4().hex[:8]
    email = f"p17-scenario-{tag}@example.com"
    tmp = Path(tempfile.mkdtemp(prefix="p17-scenario-"))
    out: dict = {"engine": url, "email": email}
    http = httpx.Client(base_url=url, timeout=60, headers={"Origin": "http://localhost:3000"})
    try:
        # 1. user + project
        reg = http.post("/auth/register", json={"email": email, "password": PASSWORD})
        login = http.post("/auth/login", json={"email": email, "password": PASSWORD, "token_in_body": True})
        session = {"Authorization": f"Bearer {login.json()['token']}"}
        out["user_id"] = reg.json()["user"]["id"]
        repo = make_repo(tmp)
        sys.path.insert(0, str(ROOT / "scripts"))
        import git_collect as gc  # noqa: PLC0415

        ident = gc.repository_identity(str(repo))
        project = http.post("/projects", json={"name": "Payments API", "repository_url": ident["repository_url"]}, headers=session)
        step(1, "user creates a project", project.status_code == 201 and project.json()["created"], project.text[:120])
        pid = project.json()["project"]["id"]
        out["project_id"] = pid
        # 2. scoped credential
        tok = http.post(f"/projects/{pid}/tokens", json={"label": "scenario-agent", "scopes": ["read", "ingest", "propose"], "ttl_days": 1}, headers=session)
        token = tok.json()["secret"]
        step(2, "project gets a scoped credential (shown once, hashed at rest)", tok.status_code == 201 and token.startswith("sbp_") and "secret" not in http.get(f"/projects/{pid}/tokens", headers=session).text)
        # 3. Git collector -> engine (real CLI, token via environment)
        env = {**os.environ, "MEMORY_ENGINE_TOKEN": token}
        col = subprocess.run([sys.executable, str(ROOT / "scripts" / "git_collect.py"), "all", "--repo", str(repo), "--server", url, "--project", "current"], capture_output=True, text=True, env=env)
        step(3, "the Git collector records history and structure", col.returncode == 0 and "1 recorded" in col.stdout, (col.stdout + col.stderr)[-200:])
        # 4-6. coding agent connects through MCP, session 1
        t0 = datetime.now(UTC) - timedelta(minutes=60)  # events carry their own (past) valid time
        async with Client(mcp_params(url, token)) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            step(4, "an agent connects through MCP and sees the high-level tools", {"get_resume_context", "record_agent_event", "search_project_memory"} <= tools and len(tools) == 17, f"{len(tools)} tools")
            first = await call(c, "get_resume_context")
            step(5, "first resume of a project with Git history but no sessions: nothing to resume, no invented work", first.get("has_history") is False and first.get("unfinished_work") == [], str(first)[:200])
            ev = lambda t, p, m: call(c, "record_agent_event", session_id="scn-1", event_type=t, payload=p, timestamp=(t0 + timedelta(minutes=m)).isoformat())  # noqa: E731
            results = []
            results.append(await ev("session_started", {"goal": "Fix the payment retry bug"}, 0))
            results.append(await ev("user_request", {"text": "Payments are not retried three times. Investigate and fix it.", "feature": "Payment retries"}, 1))
            results.append(await ev("file_inspected", {"path": "retry.py"}, 2))
            results.append(await ev("bug_discovered", {"title": "Retries stop after one attempt", "description": "MAX_RETRIES is 1 but the contract says 3", "files": ["retry.py"], "feature": "Payment retries"}, 3))
            results.append(await ev("decision", {"title": "Raise MAX_RETRIES instead of looping in the caller", "rationale": "The limit lives in one constant; callers stay simple", "files": ["retry.py"], "alternatives": ["Loop in the caller"]}, 4))
            (repo / "retry.py").write_text("MAX_RETRIES = 3\n\n\ndef should_retry(attempt: int) -> bool:\n    return attempt < MAX_RETRIES\n")
            results.append(await ev("file_modified", {"path": "retry.py", "summary": "MAX_RETRIES 1 -> 3"}, 5))
            results.append(await ev("test_run", {"command": "pytest test_retry.py", "result": "failed", "summary": "off by one", "approach": "Raise the constant only"}, 6))
            results.append(await ev("error", {"message": "AssertionError: should_retry(2) is False", "bug": "Retries stop after one attempt", "files": ["test_retry.py"], "approach": "Raise the constant only"}, 7))
            results.append(await ev("fix", {"title": "Raise the constant and keep the comparison", "outcome": "partial", "fixes_error": "AssertionError: should_retry(2) is False", "approach": "Raise the constant only"}, 8))
            (repo / "retry.py").write_text("MAX_RETRIES = 3\n\n\ndef should_retry(attempt: int) -> bool:\n    return attempt <= MAX_RETRIES\n")
            results.append(await ev("fix", {"title": "Allow the third retry", "outcome": "succeeded", "fixes_bug": "Retries stop after one attempt", "files": ["retry.py"], "approach": "Use <= for the limit"}, 9))
            results.append(await ev("test_run", {"command": "pytest test_retry.py", "result": "passed", "validates": ["Allow the third retry"], "approach": "Use <= for the limit"}, 10))
            sh(repo, "git", "add", "-A")
            sh(repo, "git", "commit", "-q", "-m", "Retry payments three times")
            head = sh(repo, "git", "rev-parse", "HEAD").strip()
            results.append(await ev("commit", {"sha": head, "message": "Retry payments three times", "files": ["retry.py"]}, 11))
            results.append(await ev("session_ended", {"outcome": "blocked", "summary": "Fix committed; the runbook still says one retry", "unfinished": ["Update the payments runbook", "Add a regression test for the third retry"]}, 12))
        errs = [r for r in results if "_error" in r]
        step(6, "session 1: every meaningful event is accepted and projected", not errs and all(r["results"][0]["projection_status"] in ("projected", "skipped") for r in results), str(errs[:1]))
        col2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "git_collect.py"), "commits", "--repo", str(repo), "--server", url], capture_output=True, text=True, env=env)
        step(7, "the new commit is recorded by the collector and is idempotent on re-run", col2.returncode == 0 and "1 recorded" in col2.stdout and "already known" in col2.stdout, col2.stdout[-160:])

        # graph, provenance, history (read back through MCP)
        async with Client(mcp_params(url, token)) as c:
            hist = await call(c, "get_project_history", limit=50)
            kinds = {i["type"] for i in hist["items"]}
            step(8, "history shows events AND Git commits in valid-time order", {"decision", "fix", "commit"} <= kinds and [i["occurred_at"] for i in hist["items"]] == sorted((i["occurred_at"] for i in hist["items"]), reverse=True))
            fh = await call(c, "get_file_history", path="retry.py")
            step(9, "file history ties commits, sessions, decisions, bugs, fixes and structure to the file", len(fh["commits"]) == 2 and fh["sessions"] and fh["decisions"] and fh["fixes"] and fh["structure"], str(list(fh)))
            dec = await call(c, "get_decisions")
            d = dec["decisions"][0]
            step(10, "the decision keeps its stated rationale and an observed (not causal) outcome", d["rationale"]["kind"] == "EXPLICIT_DECISION" and any(o["kind"] == "OBSERVED_OUTCOME" for o in d["observed_outcomes"]), str(d["observed_outcomes"])[:120])
            att = await call(c, "get_previous_attempts")
            outcomes = {x["approach"]: x["outcome"] for x in att["attempts"]}
            step(11, "previous attempts keep their explicit outcomes (partial first, then succeeded)", outcomes.get("Raise the constant only") == "partial" and outcomes.get("Use <= for the limit") == "succeeded", str(outcomes))
            bug = await call(c, "get_bug_history")
            step(12, "the bug is 'fixed' because a fix event STATED success, validated by a passing test", bug["bugs"][0]["status"] == "fixed" and bug["bugs"][0]["validated_by"], str(bug["bugs"][0]["status_basis"]))
            mem_id = d["memory_id"]
            src = await call(c, "get_source", kind="memory", ref=mem_id)
            s0 = src["sources"][0]
            step(13, "provenance of the decision memory resolves to the agent event and its session", s0["source"]["source_type"] == "agent_event" and s0["resolved"] and s0["agent_event"]["session_key"] == "scn-1", str(s0)[:160])
            cm = await call(c, "get_commit_context", commit=head[:10])
            step(14, "commit context links the commit to its session's decisions and fixes", cm["sessions"] and {"decision", "fix"} <= {i["type"] for i in cm["same_session_context"]}, str(cm.get("sessions"))[:100])
            ent = await call(c, "get_project_entity", kind="file", ref="retry.py")
            rels = {r["predicate"] for r in ent["context"]["current_relationships"]}
            step(15, "the graph reflects the work (the file is modified, inspected, changed, defined)", {"modified"} <= rels or {"changed"} <= rels, str(sorted(rels)))
            srch = await call(c, "search_project_memory", query="retry limit constant", limit=10)
            step(16, "hybrid search finds the work with evidence labels", srch["items"] and any("agent_stated" in i["evidence"] for i in srch["items"]))

        # 20-23. a SECOND agent process asks to resume
        async with Client(mcp_params(url, token)) as c2:
            r = await call(c2, "get_resume_context", task="payments retry")
            unf = [u["text"] for u in r.get("unfinished_work", []) if u["basis"] == "explicit_unfinished"]
            step(17, "session 2 (new process, no history) gets where session 1 left off", r.get("last_session", {}).get("session_id") == "scn-1" and r["last_session"]["outcome"] == "blocked")
            step(18, "unfinished work is exactly what was explicitly recorded", set(unf) == {"Update the payments runbook", "Add a regression test for the third retry"}, str(unf))
            step(19, "decisions, attempts, changed files, bug, tests and commit are reconstructed", r["recent_decisions"] and len(r["attempts"]) == 2 and r["files_modified"] == ["retry.py"] and r["known_bugs"][0]["status"] == "fixed" and r["tests_run"] and r["recent_commits"][0]["subject"] == "Retry payments three times", str(list(r)))
            step(20, "the resume is bounded", len(json.dumps(r)) < 20_000, f"{len(json.dumps(r))} bytes")
            started = await call(c2, "record_agent_event", session_id="scn-2", event_type="session_started", payload={"goal": "Update the payments runbook", "continues_session": "scn-1"})
            step(21, "session 2 starts by explicitly continuing session 1", "_error" not in started, str(started)[:100])
            again = await call(c2, "get_resume_context")
            step(22, "the continuation chain is now visible", again["last_session"]["session_id"] == "scn-2" and again["previous_sessions"][0]["session_id"] == "scn-1")

        # isolation: a stranger cannot read any of it
        other_email = f"p17-stranger-{tag}@example.com"
        http2 = httpx.Client(base_url=url, timeout=30, headers={"Origin": "http://localhost:3000"})
        http2.post("/auth/register", json={"email": other_email, "password": PASSWORD})
        stranger = {"Authorization": f"Bearer {http2.post('/auth/login', json={'email': other_email, 'password': PASSWORD, 'token_in_body': True}).json()['token']}"}
        probes = [http2.get(f"/projects/{pid}/resume", headers=stranger).status_code, http2.get(f"/projects/{pid}/decisions", headers=stranger).status_code, http2.get(f"/projects/{pid}", headers=stranger).status_code, http2.get("/memories/search", params={"q": "retry"}, headers=stranger).json().get("total", -1)]
        step(23, "another user sees none of it (404s, empty personal search)", probes == [404, 404, 404, 0], str(probes))
        out["stranger_email"] = other_email
        Path(a.credentials_file).write_text(json.dumps({"email": email, "password": PASSWORD, "project_id": pid, "project_name": "Payments API", "stranger": other_email}))
    finally:
        shutil_rm = __import__("shutil").rmtree
        shutil_rm(tmp, ignore_errors=True)
    out["steps"] = RESULTS
    Path(a.out).write_text(json.dumps(out, indent=2))
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\nscenario: {len(RESULTS) - len(failed)}/{len(RESULTS)} steps passed")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--engine", default="http://localhost:8001")
    p.add_argument("--out", default="scenario.json")
    p.add_argument("--credentials-file", default="scenario-credentials.json", help="written for the UI walk-through and cleanup; contains a synthetic password")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    t = time.time()
    code = main()
    print(f"{time.time() - t:.1f}s")
    raise SystemExit(code)
