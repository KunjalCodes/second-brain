"""Phase 9 authentication overhead benchmark (docs/PHASE9_PLAN.md Stage 16).

Measures -- against local PostgreSQL + Neo4j, through the real app over ASGI --
what authentication adds to a request:

* login at the PRODUCTION scrypt cost (default N=2^15, r=8, p=3) and at the
  OWASP N=2^17 alternative;
* the same authenticated requests in three modes:
    dev_header        the pre-Phase-9 placeholder (no DB work for identity)
    session           Bearer session lookup (+ periodic touch)
    session+limits    session + the PostgreSQL rate-limit counter
  for GET /auth/me, GET /memories/search, GET /graph/overview and
  GET /graph/entities/{id}/neighborhood;
* one rate-limit counter operation in isolation.

Synthetic data under throwaway users, cleaned up afterwards. One local
machine -- relative numbers, not a capacity claim.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.api.deps import get_app_settings, get_client, get_db, get_embeddings  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.rate_limit_service import LimitClass, RateLimiter  # noqa: E402

TRIALS = 60
PASSWORD = "benchmark passphrase value"


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _fmt(values: list[float]) -> str:
    return f"p50={statistics.median(values):7.2f}ms p95={_pct(values, 0.95):7.2f}ms"


def _settings(base: Settings, **update: object) -> Settings:
    return base.model_copy(update=update)


async def _time(fn, trials: int = TRIALS) -> list[float]:  # noqa: ANN001
    out = []
    for _ in range(trials):
        start = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


async def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    base = get_settings()
    db = Database(base)
    await db.connect()
    graph = GraphClient(base)
    await graph.connect()
    user_ids: list[str] = []
    try:
        print("Phase 9 authentication overhead -- local PostgreSQL + Neo4j, synthetic data\n")

        # -- login cost at production parameters ------------------------------------
        print("=== Login (POST /auth/login), full request ===")
        for label, n, p, maxmem in (
            ("default N=2^15 r=8 p=3", 2**15, 3, 64 * 1024 * 1024),
            ("OWASP N=2^17 r=8 p=1", 2**17, 1, 512 * 1024 * 1024),
        ):
            s = _settings(
                base,
                auth_mode="session",
                allow_user_id_header=False,
                auth_scrypt_n=n,
                auth_scrypt_p=p,
                auth_scrypt_maxmem=maxmem,
                rate_limit_enabled=False,
            )
            email = f"bench-{uuid.uuid4().hex[:8]}@example.com"
            account = await AuthService(db, s).register(email=email, password=PASSWORD)
            user_ids.append(account.user_id)
            app = create_app()
            app.dependency_overrides.update(
                {get_db: lambda: db, get_client: lambda: graph, get_app_settings: lambda s=s: s}
            )
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://b") as c:

                async def login(c=c, email=email) -> None:
                    r = await c.post("/auth/login", json={"email": email, "password": PASSWORD})
                    assert r.status_code == 200

                ok = await _time(login, trials=10)

                async def wrong(c=c, email=email) -> None:
                    await c.post(
                        "/auth/login", json={"email": email, "password": "wrong password!"}
                    )

                bad = await _time(wrong, trials=10)

                async def unknown(c=c) -> None:
                    await c.post("/auth/login", json={"email": "nobody@x.io", "password": "x" * 12})

                missing = await _time(unknown, trials=10)
            print(
                f"  {label:<24} success {_fmt(ok)} | wrong pw {_fmt(bad)} | unknown {_fmt(missing)}"
            )

        # -- per-request overhead ----------------------------------------------------
        fast = _settings(base, auth_scrypt_n=2**10, auth_scrypt_p=1)
        email = f"bench-{uuid.uuid4().hex[:8]}@example.com"
        account = await AuthService(
            db, _settings(fast, auth_mode="session", allow_user_id_header=False)
        ).register(email=email, password=PASSWORD)
        user_id = account.user_id
        user_ids.append(user_id)
        memories = MemoryService(MemoryRepository(graph))
        first = None
        for i in range(40):
            created = await memories.create(
                user_id=user_id,
                payload=MemoryCreate.model_validate(
                    {
                        "content": f"Project {i % 8} uses Tech {i}",
                        "entities": [
                            {"name": f"Project {i % 8}", "entity_type": "project"},
                            {"name": f"Tech {i}", "entity_type": "technology"},
                        ],
                        "relationships": [
                            {
                                "source": f"Project {i % 8}",
                                "predicate": "uses",
                                "target": f"Tech {i}",
                            }
                        ],
                    }
                ),
            )
            first = first or created
        entity_id = first.entities[0].id

        modes = {
            "dev_header": _settings(
                fast, auth_mode="dev_header", allow_user_id_header=True, rate_limit_enabled=False
            ),
            "session": _settings(
                fast, auth_mode="session", allow_user_id_header=False, rate_limit_enabled=False
            ),
            "session+limits": _settings(
                fast,
                auth_mode="session",
                allow_user_id_header=False,
                rate_limit_enabled=True,
                rate_limit_graph_per_user=100_000,
                rate_limit_search_per_user=100_000,
            ),
        }
        requests = [
            ("GET /auth/me", "/auth/me", {}),
            ("GET /memories/search", "/memories/search", {"q": "uses"}),
            ("GET /graph/overview", "/graph/overview", {}),
            (
                "GET /graph/.../neighborhood",
                f"/graph/entities/{entity_id}/neighborhood",
                {"hops": 2},
            ),
        ]
        results: dict[tuple[str, str], list[float]] = {}
        for mode, s in modes.items():
            app = create_app()
            app.dependency_overrides.update(
                {
                    get_db: lambda: db,
                    get_client: lambda: graph,
                    get_app_settings: lambda s=s: s,
                    get_embeddings: lambda: FakeEmbeddingClient(dimension=768),
                }
            )
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://b") as c:
                if mode == "dev_header":
                    headers = {"X-User-Id": user_id}
                else:
                    r = await c.post(
                        "/auth/login",
                        json={"email": email, "password": PASSWORD, "token_in_body": True},
                    )
                    headers = {"Authorization": f"Bearer {r.json()['token']}"}
                    c.cookies.clear()
                for label, path, params in requests:
                    for _ in range(5):  # warm-up (plan cache, pools)
                        await c.get(path, params=params, headers=headers)

                    async def call(c=c, path=path, params=params, headers=headers) -> None:
                        r = await c.get(path, params=params, headers=headers)
                        assert r.status_code == 200, r.text

                    results[(label, mode)] = await _time(call)

        print(f"\n=== Authenticated requests ({TRIALS} trials each, after warm-up) ===")
        for label, _, _ in requests:
            base_p50 = statistics.median(results[(label, "dev_header")])
            line = f"  {label:<28}"
            for mode in modes:
                vals = results[(label, mode)]
                delta = statistics.median(vals) - base_p50
                line += f" | {mode}: {_fmt(vals)}" + (
                    f" (+{delta:5.2f})" if mode != "dev_header" else ""
                )
            print(line)

        print("\n=== One rate-limit counter operation (own transaction) ===")
        limiter = RateLimiter(db, modes["session+limits"])
        key = f"bench-{uuid.uuid4().hex[:8]}"

        async def hit() -> None:
            await limiter.hit(LimitClass.GRAPH, key)

        print(f"  RateLimiter.hit            {_fmt(await _time(hit, trials=200))}")
        async with db.session() as session:
            await session.execute(
                text("DELETE FROM rate_limit_counters WHERE bucket_key LIKE '%' || :k"), {"k": key}
            )
    finally:
        async with db.session() as session:
            for uid in user_ids:
                await session.execute(
                    text("DELETE FROM rate_limit_counters WHERE bucket_key LIKE '%:u:' || :u"),
                    {"u": uid},
                )
                for table, col in (
                    ("auth_sessions", "user_id"),
                    ("user_accounts", "user_id"),
                    ("users", "id"),
                ):
                    await session.execute(text(f"DELETE FROM {table} WHERE {col} = :u"), {"u": uid})
        for uid in user_ids:
            await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": uid})
        await db.close()
        await graph.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
