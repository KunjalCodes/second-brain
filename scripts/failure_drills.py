"""Live failure drills against a real backend process (Phase 11, M13).

    cd backend
    DATABASE_URL=postgresql+asyncpg://localhost/<something>_test NEO4J_...=... \\
        uv run python ../scripts/failure_drills.py --out /tmp/drills.json

TEST DATABASES ONLY: refuses unless the database name contains "test". Seeds
its own throwaway `e2e-*@example.com` accounts (scripts/e2e_seed.py) and
deletes them afterwards.

The API runs as a subprocess whose PostgreSQL and Neo4j connections go
through TCP proxies owned by this script (each on its own thread and event
loop), so outages are injected and healed WHILE the application keeps
running. Drills:

 1  PostgreSQL unavailable          5  restart during extraction (kill -9)
 2  Neo4j unavailable               6  document ingestion interrupted
 8  expired session                 9  rate-limit exhaustion
10  invalid migration (a database one revision set behind)

Numbering follows docs/PHASE11_PLAN.md section 25. LLM unavailable / 429
(3, 4), bytes missing (7), frontend/backend version mismatch (11) and upload
too large (12) are covered by automated fault-injection tests and the OpenAPI
drift check instead -- see section 31 -- because a live provider would spend
quota (D7).
"""

# ruff: noqa: E501 - each drill's trigger/expected/actual text is kept whole, as recorded

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parent.parent / "backend"
API_PORT, PG_PROXY, NEO_PROXY = 8014, 55432, 57687
API = f"http://127.0.0.1:{API_PORT}"
RESULTS: list[dict] = []


# -- TCP proxies ------------------------------------------------------------------------


class _Proxy:
    def __init__(self, listen: int, host: str, target: int) -> None:
        self.listen, self.host, self.target = listen, host, target
        self.server: asyncio.base_events.Server | None = None
        self.conns: set[asyncio.StreamWriter] = set()

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            w.close()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection(self.host, self.target)
        except OSError:
            w.close()
            return
        self.conns |= {w, uw}
        await asyncio.gather(self._pipe(r, uw), self._pipe(ur, w))

    async def up(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen)

    async def down(self) -> None:
        if self.server:
            self.server.close()
        for w in list(self.conns):
            w.close()
        self.conns.clear()
        if self.server:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.server.wait_closed(), 5)
            self.server = None


class Proxy:
    """A _Proxy on its own thread + loop, so blocking drill calls never stall it."""

    def __init__(self, listen: int, host: str, target: int) -> None:
        self.loop = asyncio.new_event_loop()
        self._p = _Proxy(listen, host, target)
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def up(self) -> None:
        asyncio.run_coroutine_threadsafe(self._p.up(), self.loop).result(10)

    def down(self) -> None:
        asyncio.run_coroutine_threadsafe(self._p.down(), self.loop).result(15)


# -- helpers ------------------------------------------------------------------------------


def call(method: str, path: str, body: dict | None = None, token: str | None = None):  # noqa: ANN201
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw[:200].decode(errors="replace"), dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return 0, repr(e)[:200], {}


def code_of(body: object) -> str | None:
    return body.get("error", {}).get("code") if isinstance(body, dict) else None


def wait_for(pred, timeout: float, step: float = 1.0) -> bool:  # noqa: ANN001
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def record(
    n: int, name: str, trigger: str, expected: str, actual: str, ok: bool, follow_up: str = ""
) -> None:
    RESULTS.append({"n": n, "drill": name, "trigger": trigger, "expected": expected,
                    "actual": actual, "result": "PASS" if ok else "FAIL", "follow_up": follow_up})  # fmt: skip
    print(f"[{'PASS' if ok else 'FAIL'}] {n}. {name}: {actual}", flush=True)


class Stack:
    def __init__(self, database_url: str, storage: Path) -> None:
        self.database_url = database_url
        self.libpq = database_url.replace("+asyncpg", "")
        parsed = urlparse(self.libpq)
        self.pg_host, self.pg_port = parsed.hostname or "localhost", parsed.port or 5432
        self.db_name = parsed.path.lstrip("/")
        neo = urlparse(os.environ["NEO4J_URI"])
        self.neo_host, self.neo_port = neo.hostname or "localhost", neo.port or 7687
        self.storage = storage
        self.log = storage / "api.log"

    def env(self, **extra: str) -> dict:
        env = dict(os.environ)
        env.update(
            APP_ENV="development", AUTH_MODE="session", ALLOW_USER_ID_HEADER="false",
            FRONTEND_URL="http://localhost:3100", LLM_PROVIDER="fake", EMBEDDING_PROVIDER="fake",
            DATABASE_URL=f"postgresql+asyncpg://127.0.0.1:{PG_PROXY}/{self.db_name}",
            NEO4J_URI=f"bolt://127.0.0.1:{NEO_PROXY}", SWEEP_INTERVAL_SECONDS="3",
            SWEEP_INITIAL_DELAY_SECONDS="1", EXTRACTION_LEASE_SECONDS="30",
            INGESTION_LEASE_SECONDS="30", RATE_LIMIT_ENABLED="true",
            RATE_LIMIT_SEARCH_PER_USER="20", RATE_LIMIT_LOGIN_PER_EMAIL="1000",
            RATE_LIMIT_LOGIN_PER_IP="1000", NEO4J_CONNECTION_TIMEOUT="3",
            DOCUMENT_STORAGE_DIR=str(self.storage / "documents"),
        )  # fmt: skip
        env.update(extra)
        return env

    def direct_env(self) -> dict:
        return self.env(DATABASE_URL=self.database_url, NEO4J_URI=os.environ["NEO4J_URI"])

    def start_api(self, **extra: str) -> subprocess.Popen:
        with self.log.open("a") as log:
            proc = subprocess.Popen(
                ["uv", "run", "uvicorn", "app.main:app", "--port", str(API_PORT),
                 "--host", "127.0.0.1", "--no-access-log"],
                cwd=BACKEND, env=self.env(**extra), stdout=log, stderr=log, start_new_session=True,
            )  # fmt: skip
        wait_for(lambda: call("GET", "/health")[0] == 200, 30, 0.5)
        return proc


def kill9(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def sql(libpq: str, query: str, *args: object) -> list:
    import asyncpg

    async def run() -> list:
        conn = await asyncpg.connect(libpq)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def py(
    stack: Stack, code: str, *, wait: bool = True
) -> subprocess.Popen | subprocess.CompletedProcess:
    args = [sys.executable, "-c", "import sys; sys.path.insert(0, '.')\n" + code]
    if wait:
        return subprocess.run(
            args, cwd=BACKEND, env=stack.direct_env(), capture_output=True, text=True
        )
    return subprocess.Popen(args, cwd=BACKEND, env=stack.direct_env(), stdout=subprocess.PIPE,
                            text=True, start_new_session=True)  # fmt: skip


# -- drills ---------------------------------------------------------------------------------


def run(stack: Stack, seed: dict) -> None:  # noqa: PLR0915
    uid = seed["a"]["user_id"]
    pg = Proxy(PG_PROXY, stack.pg_host, stack.pg_port)
    neo = Proxy(NEO_PROXY, stack.neo_host, stack.neo_port)
    pg.up()
    neo.up()
    api = stack.start_api()
    try:
        login = call(
            "POST",
            "/auth/login",
            {"email": seed["a"]["email"], "password": seed["password"], "token_in_body": True},
        )
        token = login[1]["token"]

        # 1. PostgreSQL unavailable
        pg.down()
        ready, live = call("GET", "/health/ready"), call("GET", "/health")
        req = call("GET", "/memories/search", token=token)
        leaked = any(
            k in json.dumps(ready[1]) + json.dumps(req[1])
            for k in ("SELECT", "asyncpg", "5432", "Traceback")
        )
        pg.up()
        healed = wait_for(lambda: call("GET", "/health/ready")[0] == 200, 30)
        after = call("GET", "/memories/search", token=token)[0]
        record(1, "PostgreSQL unavailable", "cut the TCP path to PostgreSQL while the API runs",
               "liveness 200; readiness 503 (codes only); requests 503 database_unavailable, no internals; recovers without restart",
               f"live={live[0]} ready={ready[0]} {ready[1].get('checks') if isinstance(ready[1], dict) else ready[1]} "
               f"request={req[0]} {code_of(req[1])} leaked={leaked} healed={healed} after={after}",
               live[0] == 200 and ready[0] == 503 and req[0] == 503 and not leaked and healed and after == 200)  # fmt: skip

        # 2. Neo4j unavailable
        neo.down()
        ready = call("GET", "/health/ready")
        write = call(
            "POST",
            "/memories",
            {"content": f"Drill memory {uuid.uuid4().hex[:6]}", "entities": [{"name": "Atlas"}]},
            token=token,
        )
        graph = call("GET", "/graph/overview", token=token)
        leaked = any(
            k in json.dumps(write[1]) + json.dumps(graph[1])
            for k in ("bolt", "7687", "Traceback", "MATCH")
        )
        neo.up()
        healed = wait_for(lambda: call("GET", "/health/ready")[0] == 200, 45)
        rewrite = call(
            "POST",
            "/memories",
            {
                "content": f"Drill memory healed {uuid.uuid4().hex[:6]}",
                "entities": [{"name": "Atlas"}],
            },
            token=token,
        )
        record(2, "Neo4j unavailable", "cut the TCP path to Neo4j while the API runs",
               "readiness 503; memory write 503 graph_unavailable (nothing half-written); recovers without restart",
               f"ready={ready[0]} {ready[1].get('checks') if isinstance(ready[1], dict) else ''} write={write[0]} {code_of(write[1])} "
               f"graph={graph[0]} leaked={leaked} healed={healed} write_after={rewrite[0]}",
               ready[0] == 503 and write[0] == 503 and not leaked and healed and rewrite[0] == 201,
               "Extraction during a Neo4j outage: tests/integration/test_job_reliability.py (J1).")  # fmt: skip

        # 5. Restart during extraction: a separate process holds the job's lease and is killed.
        kill9(api)
        worker = py(stack, (
            "import asyncio,time,uuid\nfrom datetime import UTC,datetime\n"
            "from app.core.config import get_settings\nfrom app.db.engine import Database\n"
            "from app.models.conversation import MessageRole\n"
            "from app.extraction.trigger import maybe_trigger_extraction\n"
            "from app.repositories.conversation_repository import ConversationRepository\n"
            "from app.repositories.message_repository import MessageRepository\n"
            "from app.repositories.extraction_repository import ExtractionJobRepository\n"
            "from app.repositories.extraction_queue import ExtractionQueue\n"
            "async def m():\n"
            " s=get_settings().model_copy(update={'extraction_enabled':True});db=Database(s);await db.connect();now=datetime.now(UTC)\n"
            " async with db.session() as ses:\n"
            f"  c=await ConversationRepository(ses,user_id='{uid}').create(conversation_id=str(uuid.uuid4()),title='drill5',now=now)\n"
            f"  mr=MessageRepository(ses,user_id='{uid}')\n"
            "  msg=await mr.append(message_id=str(uuid.uuid4()),conversation_id=c.id,role=MessageRole.USER,content='Drill five: I use Grafana at work every single day.',token_count=None,now=now)\n"
            f"  await maybe_trigger_extraction(message=msg,message_repo=mr,job_repo=ExtractionJobRepository(ses,user_id='{uid}'),settings=s,now=now)\n"
            " async with db.session() as ses:\n"
            "  j=await ExtractionQueue(ses).claim_batch(limit=50,worker_id='doomed-process:1',now=now,max_attempts=3)\n"
            " print(msg.id,len(j),flush=True)\n time.sleep(600)\n"
            "asyncio.run(m())"
        ), wait=False)  # fmt: skip
        message_id, claimed = worker.stdout.readline().split()
        kill9(worker)
        held = sql(
            stack.libpq,
            "SELECT status, locked_by FROM extraction_jobs WHERE target_message_id = $1::uuid",
            message_id,
        )
        t0 = time.monotonic()
        api = stack.start_api()

        def job_done() -> bool:
            rows = sql(
                stack.libpq,
                "SELECT status FROM extraction_jobs WHERE target_message_id = $1::uuid",
                message_id,
            )
            return bool(rows) and rows[0]["status"] == "done"

        done = wait_for(job_done, 120, 2)
        record(5, "Backend restart during extraction",
               "kill -9 the API; a separate process claims the job (holds the lease) and is kill -9'd; API restarted",
               "the job held by the dead process is reclaimed once its lease expires (30 s in the drill) and completes; no manual step",
               f"held={[dict(r) for r in held]} claimed={claimed} completed={done} in {round(time.monotonic() - t0, 1)}s",
               done and bool(held) and held[0]["status"] == "in_progress" and held[0]["locked_by"] == "doomed-process:1")  # fmt: skip

        # 6. Document ingestion interrupted mid-parse.
        kill9(api)
        up = py(stack, (
            "import asyncio\nfrom app.core.config import get_settings\nfrom app.db.engine import Database\n"
            "from app.services.document_service import DocumentService\nfrom app.storage.local import LocalDocumentStorage\n"
            "async def m():\n s=get_settings();db=Database(s);await db.connect()\n"
            f" async with db.session() as ses:\n  d,_=await DocumentService(ses,LocalDocumentStorage(root_dir='{stack.storage / 'documents'}'),settings=s)"
            f".upload_document(user_id='{uid}',filename='drill.txt',content=b'Drill document about Atlas and Postgres. '*50)\n"
            "  print(d.id)\nasyncio.run(m())"
        ))  # fmt: skip
        doc_id = up.stdout.strip().splitlines()[-1]
        sql(
            stack.libpq,
            "UPDATE ingestion_jobs SET status='in_progress', attempts=1, locked_at=now(), locked_by='doomed:2' WHERE document_id=$1::uuid",
            doc_id,
        )
        sql(stack.libpq, "UPDATE documents SET status='parsing' WHERE id=$1::uuid", doc_id)
        t0 = time.monotonic()
        api = stack.start_api()
        recovered = wait_for(
            lambda: (
                (call("GET", f"/documents/{doc_id}", token=token)[1] or {}).get("status")
                in ("extracting", "complete")
            ),
            120,
            2,
        )
        final = (call("GET", f"/documents/{doc_id}", token=token)[1] or {}).get("status")
        record(6, "Document ingestion interrupted", "a worker died mid-parse (job in_progress, document 'parsing'); API restarted",
               "the ingestion sweep (previously never invoked) reclaims after the lease and the document progresses",
               f"recovered={recovered} final_status={final} in {round(time.monotonic() - t0, 1)}s", recovered)  # fmt: skip

        # 8. Expired session.
        t2 = call(
            "POST",
            "/auth/login",
            {"email": seed["a"]["email"], "password": seed["password"], "token_in_body": True},
        )[1]["token"]
        sql(
            stack.libpq,
            "UPDATE auth_sessions SET expires_at = now() - interval '1 minute' WHERE token_sha256 = $1",
            hashlib.sha256(t2.encode()).hexdigest(),
        )
        r = call("GET", "/memories/search", token=t2)
        other = call("GET", "/auth/me", token=token)[0]
        record(8, "Expired session", "set a live session's expires_at in the past",
               "401 authentication_required; the user's other sessions unaffected",
               f"expired={r[0]} {code_of(r[1])} other_session={other}", r[0] == 401 and other == 200)  # fmt: skip

        # 9. Rate-limit exhaustion.
        codes = [call("GET", "/memories/search", token=token)[0] for _ in range(22)]
        last = call("GET", "/memories/search", token=token)
        tb = call(
            "POST",
            "/auth/login",
            {"email": seed["b"]["email"], "password": seed["password"], "token_in_body": True},
        )[1]["token"]
        other = call("GET", "/memories/search", token=tb)[0]
        retry = {k.lower(): v for k, v in last[2].items()}.get("retry-after")
        record(9, "Rate-limit exhaustion", "22+ searches against a 20/min per-user limit",
               "429 with Retry-After for that user only; another user unaffected",
               f"first_429_at={codes.index(429) + 1 if 429 in codes else None} status={last[0]} retry_after={retry} other_user={other}",
               429 in codes and last[0] == 429 and bool(retry) and other == 200)  # fmt: skip
        kill9(api)

        # 10. Invalid migration: a database one revision set behind the code.
        scratch = f"ai_second_brain_migdrill_{uuid.uuid4().hex[:8]}_test"
        admin = stack.libpq.rsplit("/", 1)[0] + "/postgres"
        sql(admin, f'CREATE DATABASE "{scratch}"')
        try:
            behind_url = stack.database_url.rsplit("/", 1)[0] + f"/{scratch}"
            subprocess.run(["uv", "run", "alembic", "upgrade", "ec358e32d566"], cwd=BACKEND,
                           env={**stack.direct_env(), "DATABASE_URL": behind_url}, capture_output=True, check=True)  # fmt: skip
            api = stack.start_api(
                DATABASE_URL=f"postgresql+asyncpg://127.0.0.1:{PG_PROXY}/{scratch}"
            )
            ready = call("GET", "/health/ready")
            record(10, "Invalid migration / schema mismatch", "start the API against a database at an older revision (a deploy that skipped `alembic upgrade`)",
                   "readiness 503 with migrations=mismatch, so the platform routes no traffic",
                   f"ready={ready[0]} checks={ready[1].get('checks') if isinstance(ready[1], dict) else ready[1]}",
                   ready[0] == 503 and isinstance(ready[1], dict) and ready[1]["checks"]["migrations"] == "mismatch")  # fmt: skip
            kill9(api)
        finally:
            sql(admin, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
    finally:
        kill9(api)
        pg.down()
        neo.down()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, default=Path("/tmp/second-brain-drills"))
    args = p.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    if "test" not in urlparse(database_url.replace("+asyncpg", "")).path:
        print(
            "ERROR: DATABASE_URL must point at a TEST database (name containing 'test')",
            file=sys.stderr,
        )
        return 2
    args.work_dir.mkdir(parents=True, exist_ok=True)
    seed_file = args.work_dir / "seed.json"
    subprocess.run(
        ["uv", "run", "python", "../scripts/e2e_seed.py", "seed", "--out", str(seed_file)],
        cwd=BACKEND,
        check=True,
    )
    try:
        run(Stack(database_url, args.work_dir), json.loads(seed_file.read_text()))
    finally:
        subprocess.run(
            [
                "uv",
                "run",
                "python",
                "../scripts/e2e_seed.py",
                "teardown",
                "--state",
                str(seed_file),
            ],
            cwd=BACKEND,
        )
    args.out.write_text(json.dumps(RESULTS, indent=2))
    passed = sum(r["result"] == "PASS" for r in RESULTS)
    print(f"{passed}/{len(RESULTS)} drills passed -> {args.out}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
