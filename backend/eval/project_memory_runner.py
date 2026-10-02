# ruff: noqa: E501 - task tables are kept one per line
"""Deterministic project-memory evaluation (Phase 16, M28).

    cd backend && uv run python eval/project_memory_runner.py [--json out.json]

Builds a REAL temporary git repository from the dataset, reads it with the REAL local
collector, records a scripted agent history (an unrelated distractor session, then a
two-session fix-and-continue scenario) through the REAL HTTP routes with a project token,
and checks categories A-S. No LLM; embeddings are the deterministic fake. Every
synthetic row and node is deleted. All numbers are computed here, none are hardcoded.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["AUTH_MODE"] = "dev_header"
os.environ["ALLOW_USER_ID_HEADER"] = "true"

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.api.deps import get_client, get_db, get_embeddings  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.main import app  # noqa: E402
from app.repositories.user_repository import UserRepository  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATASET = Path(__file__).parent / "datasets" / "project_memory_v1.json"
spec = importlib.util.spec_from_file_location("git_collect", ROOT / "scripts" / "git_collect.py")
gc = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(gc)  # type: ignore[union-attr]

NOW = datetime.now(UTC).replace(microsecond=0)


def git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, **({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {})}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


def build_repo(root: Path, ds: dict) -> Path:
    repo = root / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Eval Author")
    git(repo, "config", "user.email", "eval@example.com")
    git(repo, "remote", "add", "origin", ds["remote"])
    for c in ds["commits"]:
        for path, content in c["files"].items():
            f = repo / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        stamp = (NOW + timedelta(days=c["day"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        git(repo, "add", "-A", "--", ".", date=stamp)
        git(repo, "commit", "-q", "-m", c["msg"], date=stamp)
    return repo


class Report:
    def __init__(self) -> None:
        self.tasks: list[dict] = []
        self.sets: list[tuple[float, float]] = []  # (precision, recall)
        self.latency: dict[str, list[float]] = {}
        self.sizes: dict[str, list[int]] = {}

    def task(self, cat: str, name: str, ok: bool, detail: str = "") -> None:
        self.tasks.append({"category": cat, "task": name, "passed": bool(ok), "detail": detail})

    def pr(self, expected: set, got: set) -> tuple[float, float]:
        p = len(expected & got) / len(got) if got else (1.0 if not expected else 0.0)
        r = len(expected & got) / len(expected) if expected else 1.0
        self.sets.append((p, r))
        return p, r


class Eval:
    def __init__(self, db: Database, graph: GraphClient, ds: dict, repo: Path) -> None:
        self.db, self.graph, self.ds, self.repo = db, graph, ds, repo
        self.rep = Report()
        self.uid = f"eval-pm-{uuid.uuid4().hex[:10]}"
        self.other = f"eval-pm-other-{uuid.uuid4().hex[:10]}"
        self.http = AsyncClient(transport=ASGITransport(app=app), base_url="http://eval")
        self.spaces: list[str] = []
        self.owners = [self.uid, self.other]

    async def call(self, method: str, url: str, who: str, key: str | None = None, **kw):  # noqa: ANN003, ANN202
        headers = {**kw.pop("headers", {})}
        if who.startswith("sbp_"):
            headers["Authorization"] = f"Bearer {who}"
        else:
            headers["X-User-Id"] = who
        t0 = time.perf_counter()
        r = await self.http.request(method, url, headers=headers, **kw)
        dt = (time.perf_counter() - t0) * 1000
        if key:
            self.rep.latency.setdefault(key, []).append(dt)
            self.rep.sizes.setdefault(key, []).append(len(r.content))
        return r

    async def setup(self) -> None:
        async with self.db.session() as s:
            for u in self.owners:
                await UserRepository(s).ensure_exists(u, now=NOW)
        ident = gc.repository_identity(str(self.repo))
        self.identity = ident
        r = await self.call("POST", "/projects", self.uid, json={"name": self.ds["project_name"], "repository_url": ident["repository_url"]})
        self.pid = r.json()["project"]["id"]
        tok = await self.call("POST", f"/projects/{self.pid}/tokens", self.uid, json={"label": "eval", "scopes": ["read", "ingest", "propose"]})
        self.tok = tok.json()["secret"]
        r2 = await self.call("POST", "/projects", self.other, json={"name": "Other Project", "repository_url": "https://github.com/eval-other/billing"})
        self.pid2 = r2.json()["project"]["id"]
        t2 = await self.call("POST", f"/projects/{self.pid2}/tokens", self.other, json={"label": "eval", "scopes": ["read", "ingest"]})
        self.tok2 = t2.json()["secret"]
        async with self.db.session() as s:
            self.spaces = [r[0] for r in (await s.execute(text("SELECT space_id FROM projects WHERE owner_user_id IN (:a,:b)"), {"a": self.uid, "b": self.other})).all()]
            self.space = (await s.execute(text("SELECT space_id FROM projects WHERE id=:p"), {"p": self.pid})).scalar_one()

    # -- ingestion ------------------------------------------------------------------

    async def push_git(self) -> list[dict]:
        commits, branches = gc.collect_commits(str(self.repo), 50)
        commits.reverse()
        out: list[dict] = []
        for i in range(0, len(commits), 25):
            r = await self.call("POST", f"/projects/{self.pid}/git/commits", self.tok, "ingest_git_batch", json={"identity": self.identity, "branches": branches, "commits": commits[i : i + 25]})
            out += r.json()["results"]
        sha, files, _ = gc.collect_structure(str(self.repo), "HEAD", gc.DEFAULT_EXCLUDES, 1000)
        r = await self.call("POST", f"/projects/{self.pid}/git/structure", self.tok, "ingest_structure", json={"identity": self.identity, "commit_sha": sha, "files": files})
        self.structure_result = r.json()
        return out

    def event_body(self, e: dict, head: str) -> dict:
        p = json.loads(json.dumps(e["p"]).replace("@HEAD", head))
        return {"session_id": e["s"], "event_type": e["type"], "timestamp": (NOW + timedelta(minutes=e["m"])).strftime("%Y-%m-%dT%H:%M:%SZ"), "payload": p, "event_id": f"eval-{e['s']}-{abs(e['m'])}"}

    async def push_events(self) -> list[dict]:
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.head = head
        evs = [self.event_body(e, head) for e in self.ds["events"]]
        out: list[dict] = []
        for i in range(0, len(evs), 25):
            r = await self.call("POST", f"/projects/{self.pid}/events", self.tok, "ingest_event_batch", json={"events": evs[i : i + 25]})
            assert r.status_code == 200, r.text
            out += r.json()["results"]
        self.event_results = out
        self.events = evs
        return out

    async def get(self, route: str, key: str | None = None, token: str | None = None, **params):  # noqa: ANN003, ANN202
        r = await self.call("GET", f"/projects/{self.pid}{route}", token or self.tok, key, params=params)
        return r

    async def count(self, table: str, space: str | None = None) -> int:
        async with self.db.session() as s:
            return (await s.execute(text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": space or self.space})).scalar_one()  # noqa: S608

    async def mem_count(self) -> int:
        return (await self.graph.read("MATCH (m:Memory {user_id:$u}) RETURN count(m) AS n", {"u": self.space}))[0]["n"]

    # -- tasks ----------------------------------------------------------------------

    async def run(self) -> None:
        R, ex = self.rep, self.ds["expect"]
        # A. registration
        again = await self.call("POST", "/projects", self.uid, json={"name": "renamed", "repository_url": "git@github.com:Eval-Acme/Auth-Service.git"})
        R.task("A", "same repository, another spelling -> same project", again.status_code == 200 and again.json()["project"]["id"] == self.pid and not again.json()["created"])
        bad = await self.call("POST", "/projects", self.uid, json={"name": "p", "repository_url": "/Users/me/code/repo"})
        R.task("A", "a filesystem path is refused", bad.status_code == 422)
        # B. git ingestion + M. idempotency of git
        res = await self.push_git()
        R.task("B", "every commit is recorded and projected", len(res) == 3 and all(r["status"] == "recorded" and r["projection_status"] == "projected" for r in res))
        before = (await self.count("git_commits"), await self.mem_count())
        again_git = await self.push_git()
        R.task("M", "re-pushing the same history changes nothing", all(r["status"] == "duplicate" for r in again_git) and before == (await self.count("git_commits"), await self.mem_count()), f"{before}")
        self.idempotency = {"git_rows_delta": (await self.count("git_commits")) - before[0], "memory_delta": (await self.mem_count()) - before[1]}
        # agent history
        res_e = await self.push_events()
        bad_proj = [r for r in res_e if r["projection_status"] not in ("projected", "skipped")]
        R.task("B", "every agent event is recorded and projected", not bad_proj, str(bad_proj[:2]))
        ev_before = (await self.count("agent_events"), await self.mem_count())
        replay = await self.call("POST", f"/projects/{self.pid}/events", self.tok, json={"events": self.events[:25]})
        replay2 = await self.call("POST", f"/projects/{self.pid}/events", self.tok, json={"events": self.events})
        ev_after = (await self.count("agent_events"), await self.mem_count())
        R.task("M", "replaying events (a batch, then everything) creates nothing", replay.status_code == 200 and replay2.status_code == 200 and ev_before == ev_after and all(r["status"] == "duplicate" for r in replay2.json()["results"]), f"{ev_before}->{ev_after}")
        self.idempotency.update({"event_rows_delta": ev_after[0] - ev_before[0], "memory_delta_events": ev_after[1] - ev_before[1]})
        # C. file history
        for path, want in ex["file_history"].items():
            r = (await self.get("/files/history", "file_history", path=path)).json()
            got_c, got_s, got_d = {c["subject"] for c in r["commits"]}, {s["session_id"] for s in r["sessions"]}, {d["title"] for d in r["decisions"]}
            p1, r1 = R.pr(set(want["commits"]), got_c); p2, r2 = R.pr(set(want["sessions"]), got_s); p3, r3 = R.pr(set(want["decisions"]), got_d)  # noqa: E702
            R.task("C", f"file history {path}", min(p1, r1, p2, r2, p3, r3) == 1.0, f"commits P{p1:.2f}/R{r1:.2f} sessions P{p2:.2f}/R{r2:.2f} decisions P{p3:.2f}/R{r3:.2f}")
        # D. commit context
        c = (await self.get(f"/commits/{self.head[:10]}", "commit_context")).json()
        R.task("D", "commit context: parent, session, same-session decisions", c["commit"]["subject"] == "Extend session TTL" and len(c["parents"]) == 1 and [s["session_id"] for s in c["sessions"]] == ["s1"] and {"decision", "fix"} <= {i["type"] for i in c["same_session_context"]} and c["basis"] == "same_session")
        # E / Q. decisions + supersession
        d = (await self.get("/decisions", "decisions", limit=50)).json()["decisions"]
        status = {x["title"]: x["status"] for x in d}
        p, r = R.pr(set(ex["decisions"]), set(status))
        R.task("E", "decision history lists each decision with its status", status == ex["decisions"] and p == r == 1.0, str(status))
        by = {x["title"]: x for x in d}
        a, b = by["Refresh tokens on every request"], by["Extend the TTL instead"]
        R.task("Q", "a superseded decision stays traceable both ways", a["superseded_by"]["title"] == b["title"] and b["supersedes"]["title"] == a["title"] and a["rationale"]["kind"] == "EXPLICIT_DECISION" and a["memory_id"] != b["memory_id"])
        R.task("E", "no rationale is ever fabricated", all((x["rationale"] is None) == (x["title"] not in {"Refresh tokens on every request", "Extend the TTL instead", "Use CSS variables for invoice colours"}) for x in d))
        # F. bugs
        bugs = {x["title"]: x["status"] for x in (await self.get("/bugs", "bug_history")).json()["bugs"]}
        R.task("F", "bug statuses come only from stated outcomes", bugs == ex["bugs"], str(bugs))
        # I. attempts
        att = {x["approach"]: x["outcome"] for x in (await self.get("/attempts", "attempts")).json()["attempts"]}
        p, r = R.pr(set(ex["attempts"]), set(att))
        R.task("I", "previous attempts with explicit outcomes", att == ex["attempts"] and p == r == 1.0, str(att))
        # G / N. sessions
        s1 = (await self.get("/sessions/s1", "session_detail")).json()
        s2 = (await self.get("/sessions/s2")).json()
        R.task("G", "session detail reconstructs files, decisions, fixes, commit", s1["files_modified"] == ["src/auth/session.py"] and len(s1["decisions"]) == 2 and s1["commits"] == [self.head] and s1["session"]["outcome"] == "blocked")
        R.task("N", "a session records that it continues another", s2["continues_session"] == "s1")
        # R. unfinished
        res_r = (await self.get("/resume", "resume")).json()
        explicit = [u["text"] for u in res_r["unfinished_work"] if u["basis"] == "explicit_unfinished"]
        p, r = R.pr(set(ex["unfinished"]), set(explicit))
        R.task("R", "explicit unfinished work is returned, and only that kind is called explicit", p == r == 1.0, f"P{p:.2f} R{r:.2f}")
        # H / S. resume
        want = ex["resume"]
        got_dec = {d["title"] for d in res_r["recent_decisions"]}
        got_att = {a["approach"] for a in res_r["attempts"]}
        ok = want["must_have_decisions"] and set(want["must_have_decisions"]) <= got_dec and set(want["must_have_attempts"]) <= got_att and set(want["must_have_files_modified"]) <= set(res_r["files_modified"]) and res_r["previous_sessions"][0]["session_id"] == want["previous_session"]
        R.task("H", "resume context contains where I left off, decisions, attempts, changed files", ok)
        items = [("decision", d["title"]) for d in res_r["recent_decisions"]] + [("bug", b["title"]) for b in res_r["known_bugs"]] + [("attempt", a["approach"]) for a in res_r["attempts"]] + [("error", e["title"]) for e in res_r["previous_errors"]]
        noise = [t for _, t in items if t in set(want["distractors"])]
        self.unnecessary = {"items": len(items), "unrelated": len(noise), "rate": round(len(noise) / len(items), 3) if items else 0.0, "unrelated_titles": noise}
        R.task("S", "current state: active decision first, bug fixed, last session s2", res_r["recent_decisions"][0]["title"] == "Extend the TTL instead" and res_r["last_session"]["session_id"] == "s2" and any(b["title"] == "Session timeout" and b["status"] == "fixed" for b in res_r["known_bugs"]))
        # J. multi-hop graph
        ent = await self.call("GET", "/graph/paths", self.uid, "graph_paths", params={"source_entity_id": await self.entity("session:s1"), "target_entity_id": await self.entity(f"commit:{self.head[:12]}")}, headers={"X-Memory-Space": self.pid})
        paths = ent.json().get("paths", [])
        R.task("J", "multi-hop: session -> file -> commit is found, labelled inferred", bool(paths) and paths[0]["basis"] in ("inferred_from_graph", "directly_stated") and paths[0]["length"] <= 3, str(ent.json().get("reason")))
        g = (await self.get("/graph", "graph", kind="file", ref="src/auth/session.py", hops=2, max_nodes=40)).json()["graph"]
        R.task("J", "graph neighbourhood is bounded", len(g["nodes"]) <= 40 and len(g["edges"]) <= 120)
        # O. provenance
        sample = (await self.get("/history", None, limit=50)).json()["items"]
        checked = ok_prov = 0
        bad_prov: list[dict] = []
        for it in sample:
            if not it.get("memory_id"):
                continue
            checked += 1
            pr_ = (await self.get(f"/sources/memory/{it['memory_id']}", "provenance")).json()
            srcs = pr_["sources"]
            want_t = "agent_event" if it["kind"] == "event" else "git_commit"
            good = bool(srcs) and all(x["resolved"] is True for x in srcs) and want_t in {x["source"]["source_type"] for x in srcs}
            ok_prov += good
            if not good:
                bad_prov.append({"title": it["title"], "type": it["type"], "sources": [(x["source"]["source_type"], x["resolved"]) for x in srcs]})
        self.provenance = {"checked": checked, "correct": ok_prov, "accuracy": round(ok_prov / checked, 3) if checked else 0.0, "failures": bad_prov}
        R.task("O", "every sampled memory resolves to its stored original of the right type", checked > 0 and ok_prov == checked, str(self.provenance))
        # P. temporal
        win = (await self.get("/history", None, kinds="events", since=(NOW + timedelta(minutes=-8001)).strftime("%Y-%m-%dT%H:%M:%SZ"), until=(NOW + timedelta(minutes=-7000)).strftime("%Y-%m-%dT%H:%M:%SZ"), limit=50)).json()["items"]
        R.task("P", "a valid-time window returns only that era (the distractor session)", bool(win) and all(i["session_id"] == "s0" for i in win), str(len(win)))
        recorded_vs_valid = all(i["occurred_at"] != i["recorded_at"] for i in sample if i["kind"] == "event")
        R.task("P", "valid time and recorded time are different fields", recorded_vs_valid)
        # search
        for q in self.ds["search"]:
            r = (await self.get("/memory/search", "search", query=q["query"], limit=8)).json()["items"]
            blob = " ".join(i["content"] for i in r)
            ok_s = any(x.lower() in blob.lower() for x in q["expect_any"]) and not any(f.lower() in " ".join(i["content"] for i in r[:3]).lower() for f in q["forbid"])
            R.task("search", f"search: {q['query']}", ok_s)
        # K. cross-project / L. cross-user isolation
        leaks = 0
        for route in ["/decisions", "/bugs", "/attempts", "/history", "/sessions", "/resume", "/memory/search?query=session"]:
            base = route.split("?")[0]
            r = await self.call("GET", f"/projects/{self.pid2}{base}", self.tok2, params={"query": "session"} if "search" in route else None)
            body = json.dumps(r.json()) if r.status_code == 200 else ""
            leaks += any(w in body for w in ("Extend the TTL", "Session timeout", "session:s1", "src/auth/session.py"))
        R.task("K", "another project (same account) sees none of this project", leaks == 0, f"leaks={leaks}")
        wrong = await self.call("GET", f"/projects/{self.pid}/decisions", self.tok2)
        wrong_user = await self.call("GET", f"/projects/{self.pid}/decisions", self.other)
        R.task("K", "another project's token cannot name this project", wrong.status_code == 404)
        R.task("L", "another user cannot read, ingest or resume this project", wrong_user.status_code == 404 and (await self.call("GET", f"/projects/{self.pid}/resume", self.other)).status_code == 404 and (await self.call("POST", f"/projects/{self.pid}/events", self.other, json={"events": self.events[:1]})).status_code == 404)
        self.isolation = {"leaks": leaks, "cross_project_status": wrong.status_code, "cross_user_status": wrong_user.status_code}

    async def entity(self, name: str) -> str:
        rows = await self.graph.read("MATCH (e:Entity {user_id:$u, normalized_name:$n}) RETURN e.id AS id", {"u": self.space, "n": " ".join(name.lower().split())})
        return rows[0]["id"]

    async def cleanup(self) -> None:
        async with self.db.session() as s:
            for u in self.owners:
                spaces = [r[0] for r in (await s.execute(text("SELECT space_id FROM projects WHERE owner_user_id=:u"), {"u": u})).all()]
                await s.execute(text("DELETE FROM projects WHERE owner_user_id=:u"), {"u": u})
                for sp in spaces:
                    await s.execute(text("DELETE FROM memory_embeddings WHERE user_id=:u"), {"u": sp})
                    await s.execute(text("DELETE FROM entity_merge_candidates WHERE user_id=:u"), {"u": sp})
                    await s.execute(text("DELETE FROM users WHERE id=:u"), {"u": sp})
                await s.execute(text("DELETE FROM rate_limit_counters WHERE bucket_key LIKE '%:u:' || :u"), {"u": u})
                await s.execute(text("DELETE FROM users WHERE id=:u"), {"u": u})
        for sp in self.spaces + self.owners:
            while True:
                r = await self.graph.write("MATCH (n {user_id: $u}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c", {"u": sp})
                if not r or r[0]["c"] == 0:
                    break
        await self.http.aclose()


def summarise(ev: Eval) -> dict:
    R = ev.rep
    by_cat: dict[str, list[bool]] = {}
    for t in R.tasks:
        by_cat.setdefault(t["category"], []).append(t["passed"])
    med = lambda xs: round(statistics.median(xs), 1) if xs else None  # noqa: E731
    return {
        "tasks_passed": sum(t["passed"] for t in R.tasks), "tasks_total": len(R.tasks),
        "categories": {c: f"{sum(v)}/{len(v)}" for c, v in sorted(by_cat.items())},
        "retrieval_precision": round(statistics.mean(p for p, _ in R.sets), 3) if R.sets else None,
        "retrieval_recall": round(statistics.mean(r for _, r in R.sets), 3) if R.sets else None,
        "provenance": ev.provenance, "idempotency": ev.idempotency, "isolation": ev.isolation,
        "unnecessary_context": ev.unnecessary,
        "latency_ms_median": {k: med(v) for k, v in sorted(R.latency.items())},
        "payload_bytes_median": {k: int(statistics.median(v)) for k, v in sorted(R.sizes.items())},
        "failures": [t for t in R.tasks if not t["passed"]],
    }


async def main_async(out: Path | None) -> int:
    settings = get_settings()
    db, graph = Database(settings), GraphClient(settings)
    await db.connect()
    await graph.connect()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient(dimension=768)
    ds = json.loads(DATASET.read_text())
    tmp = tempfile.mkdtemp(prefix="pm-eval-")
    ev: Eval | None = None
    try:
        ev = Eval(db, graph, ds, build_repo(Path(tmp), ds))
        await ev.setup()
        await ev.run()
        summary = summarise(ev)
        for t in ev.rep.tasks:
            print(f"  {'PASS' if t['passed'] else 'FAIL'}  {t['category']}  {t['task']}  {t['detail'] if not t['passed'] else ''}")
        print(f"\nproject memory: {summary['tasks_passed']}/{summary['tasks_total']} tasks passed")
        for k in ("categories", "retrieval_precision", "retrieval_recall", "provenance", "idempotency", "isolation", "unnecessary_context", "latency_ms_median", "payload_bytes_median"):
            print(f"{k}: {summary[k]}")
        if out:
            out.write_text(json.dumps(summary, indent=2, default=str))
        return 0 if summary["tasks_passed"] == summary["tasks_total"] else 1
    finally:
        if ev is not None:
            await ev.cleanup()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        app.dependency_overrides.clear()
        await db.close()
        await graph.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", type=Path)
    return asyncio.run(main_async(p.parse_args().json))


if __name__ == "__main__":
    raise SystemExit(main())
