# ruff: noqa: E501 - result tables kept one per line
"""Phase 16 (M30) performance of project ingestion and queries at scale.

    cd backend && uv run python eval/project_memory_benchmark.py [--files 100,1000,10000] [--commits N] [--sessions N] [--json out.json]

Synthetic, deterministic projects (no real repository needed): N files in a directory tree,
a long commit history touching them, many agent sessions, and a dense co-change pattern.
Times the REAL routes in-process (ASGI, project token): ingestion throughput per kind, then
median/max latency and payload size of resume, file history, commit context, graph and
search. Local, one Neo4j Community instance, fake embeddings: characterisation only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
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

NOW = datetime.now(UTC).replace(microsecond=0)
RUNS = 5


def sha(i: int) -> str:
    return hashlib.sha1(f"commit-{i}".encode()).hexdigest()  # noqa: S324 - synthetic ids


def path_of(i: int) -> str:
    return f"pkg{i % 20}/mod{(i // 20) % 50}/file_{i}.py"


async def timed(c: AsyncClient, hdr: dict, method: str, url: str, **kw):  # noqa: ANN003, ANN202
    lat, size, status = [], 0, 0
    for _ in range(RUNS):
        t0 = time.perf_counter()
        r = await c.request(method, url, headers=hdr, **kw)
        lat.append((time.perf_counter() - t0) * 1000)
        size, status = len(r.content), r.status_code
    return {"status": status, "median_ms": round(statistics.median(lat), 1), "max_ms": round(max(lat), 1), "bytes": size}


async def bench(db: Database, graph: GraphClient, files: int, commits: int, sessions: int) -> dict:
    uid = f"bench-pm-{files}-{uuid.uuid4().hex[:6]}"
    async with db.session() as s:
        await UserRepository(s).ensure_exists(uid, now=NOW)
    out: dict = {"files": files, "commits": commits, "sessions": sessions}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://b") as c:
        u = {"X-User-Id": uid}
        remote = f"https://github.com/bench/p{files}"
        pid = (await c.post("/projects", json={"name": f"Bench {files}", "repository_url": remote}, headers=u)).json()["project"]["id"]
        tok = (await c.post(f"/projects/{pid}/tokens", json={"label": "b", "scopes": ["read", "ingest"]}, headers=u)).json()["secret"]
        hdr = {"Authorization": f"Bearer {tok}"}
        ident = {"repository_url": remote}
        try:
            # commits
            t0 = time.perf_counter()
            for i in range(0, commits, 25):
                batch = []
                for k in range(i, min(i + 25, commits)):
                    touched = [path_of((k * 7 + j) % files) for j in range(4)] + [path_of(k % files)]
                    batch.append({"sha": sha(k), "parents": [sha(k - 1)] if k else [], "committed_at": (NOW - timedelta(days=commits - k)).isoformat(),
                                  "subject": f"Change {k}: update module", "branches": ["main"], "files_changed_count": len(set(touched)),
                                  "files": [{"path": p, "status": "M", "additions": 3, "deletions": 1} for p in sorted(set(touched))]})
                r = await c.post(f"/projects/{pid}/git/commits", json={"identity": ident, "commits": batch}, headers=hdr)
                assert r.status_code == 200, r.text
            out["ingest_commits_per_s"] = round(commits / (time.perf_counter() - t0), 1)
            # structure
            t0 = time.perf_counter()
            for i in range(0, files, 100):
                batch = [{"path": path_of(k), "language": "python", "symbols": [{"name": f"Cls{k}", "kind": "class", "line": 1}, {"name": f"Cls{k}.run", "kind": "method", "line": 2}],
                          "imports": [path_of((k + 1) % files)] if files > 1 else [], "dependencies": ["requests"]} for k in range(i, min(i + 100, files))]
                r = await c.post(f"/projects/{pid}/git/structure", json={"identity": ident, "commit_sha": sha(commits - 1), "files": batch}, headers=hdr)
                assert r.status_code == 200, r.text
            out["ingest_structure_files_per_s"] = round(files / (time.perf_counter() - t0), 1)
            # sessions / events
            t0 = time.perf_counter()
            n_events = 0
            for sidx in range(sessions):
                key = f"b{sidx}"
                base = NOW - timedelta(hours=sessions - sidx)
                ev = [{"session_id": key, "event_type": "session_started", "timestamp": base.isoformat(), "payload": {"goal": f"Task {sidx}"}}]
                for j in range(8):
                    p = path_of((sidx * 13 + j) % files)
                    kind = ["file_inspected", "file_modified", "decision", "error", "fix", "test_run", "file_modified", "task_completed"][j]
                    payload = {"file_inspected": {"path": p}, "file_modified": {"path": p}, "decision": {"title": f"Decision {sidx}", "rationale": "because", "files": [p]},
                               "error": {"message": f"Err {sidx}", "approach": f"A{sidx}"}, "fix": {"title": f"Fix {sidx}", "outcome": "succeeded", "fixes_error": f"Err {sidx}", "approach": f"A{sidx}"},
                               "test_run": {"command": "pytest", "result": "passed", "validates": [f"Fix {sidx}"]}, "task_completed": {"summary": f"Done {sidx}"}}[kind]
                    ev.append({"session_id": key, "event_type": kind, "timestamp": (base + timedelta(minutes=j + 1)).isoformat(), "payload": payload})
                for k in range(0, len(ev), 25):
                    r = await c.post(f"/projects/{pid}/events", json={"events": ev[k : k + 25]}, headers=hdr)
                    assert r.status_code == 200, r.text
                n_events += len(ev)
            out["ingest_events_per_s"] = round(n_events / (time.perf_counter() - t0), 1)
            hot = path_of(0)
            out["resume"] = await timed(c, hdr, "GET", f"/projects/{pid}/resume")
            out["file_history"] = await timed(c, hdr, "GET", f"/projects/{pid}/files/history", params={"path": hot})
            out["related_changes"] = await timed(c, hdr, "GET", f"/projects/{pid}/related-changes", params={"path": hot})
            out["commit_context"] = await timed(c, hdr, "GET", f"/projects/{pid}/commits/{sha(commits // 2)[:12]}")
            out["history_page"] = await timed(c, hdr, "GET", f"/projects/{pid}/history", params={"limit": 50})
            out["decisions"] = await timed(c, hdr, "GET", f"/projects/{pid}/decisions", params={"limit": 20})
            out["attempts"] = await timed(c, hdr, "GET", f"/projects/{pid}/attempts")
            out["graph_2hop_file"] = await timed(c, hdr, "GET", f"/projects/{pid}/graph", params={"kind": "file", "ref": hot, "hops": 2, "max_nodes": 60})
            out["entity_context_file"] = await timed(c, hdr, "GET", f"/projects/{pid}/entity", params={"kind": "file", "ref": hot})
            out["search"] = await timed(c, hdr, "GET", f"/projects/{pid}/memory/search", params={"query": "update module", "limit": 10})
            async with db.session() as s:
                out["rows"] = {t: (await s.execute(text(f"SELECT count(*) FROM {t} WHERE project_id = :p"), {"p": pid})).scalar_one() for t in ("git_commits", "agent_sessions", "agent_events")}  # noqa: S608
            out["graph_memories"] = (await graph.read("MATCH (m:Memory {user_id: $u}) RETURN count(m) AS n", {"u": await space(db, pid)}))[0]["n"]
        finally:
            sp = await space(db, pid)
            async with db.session() as s:
                await s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
                await s.execute(text("DELETE FROM memory_embeddings WHERE user_id = :u"), {"u": sp})
                await s.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": sp, "b": uid})
            for owner in (sp, uid):
                while True:
                    r = await graph.write("MATCH (n {user_id: $u}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c", {"u": owner})
                    if not r or r[0]["c"] == 0:
                        break
    return out


async def space(db: Database, pid: str) -> str:
    async with db.session() as s:
        row = (await s.execute(text("SELECT space_id FROM projects WHERE id = :p"), {"p": pid})).scalar_one_or_none()
    return row or "gone"


async def main_async(sizes: list[int], commits: int, sessions: int, out: Path | None) -> None:
    settings = get_settings()
    db, graph = Database(settings), GraphClient(settings)
    await db.connect()
    await graph.connect()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_client] = lambda: graph
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddingClient(dimension=768)
    results = []
    try:
        for n in sizes:
            r = await bench(db, graph, n, min(commits, max(n, 50)), sessions)
            results.append(r)
            print(f"=== {n} files / {r['commits']} commits / {sessions} sessions")
            for k, v in r.items():
                print(f"  {k:<30} {v}")
    finally:
        app.dependency_overrides.clear()
        await db.close()
        await graph.close()
    if out:
        out.write_text(json.dumps(results, indent=2))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--files", default="100,1000")
    p.add_argument("--commits", type=int, default=500)
    p.add_argument("--sessions", type=int, default=40)
    p.add_argument("--json", type=Path)
    a = p.parse_args()
    asyncio.run(main_async([int(x) for x in a.files.split(",")], a.commits, a.sessions, a.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
