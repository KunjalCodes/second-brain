"""Controlled HTTP load characterization (docs/PHASE11_PLAN.md section 17).

Closed-loop virtual users against a RUNNING backend: each user sends the next
request as soon as the previous one returns, for a fixed duration after a
warm-up. Reports p50 / p95 / max latency, error rate and throughput per
endpoint and concurrency level as JSON.

    uv run python eval/load_test.py --base-url http://127.0.0.1:8011 \\
        --seed-file ../frontend/e2e/.auth/seed.json --out /tmp/load.json

Authentication uses Bearer tokens (`token_in_body`), obtained once per virtual
user before measuring; login itself is measured separately (`--scenarios login`).
Data comes from `scripts/e2e_seed.py` (synthetic accounts only).

This is a local characterization tool. Its numbers describe one machine and
one configuration -- they are NOT capacity claims.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx


@dataclass
class Sample:
    latencies_ms: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    started: float = 0.0
    finished: float = 0.0

    def summary(self) -> dict[str, object]:
        ok = sorted(self.latencies_ms)
        total = len(ok) + sum(self.errors.values())
        elapsed = max(self.finished - self.started, 1e-9)

        def pct(q: float) -> float | None:
            if not ok:
                return None
            return round(ok[min(len(ok) - 1, round(q * (len(ok) - 1)))], 1)

        return {
            "requests": total,
            "ok": len(ok),
            "errors": self.errors,
            "error_rate": round(sum(self.errors.values()) / total, 4) if total else None,
            "throughput_rps": round(total / elapsed, 1),
            "p50_ms": pct(0.5),
            "p95_ms": pct(0.95),
            "max_ms": round(ok[-1], 1) if ok else None,
            "mean_ms": round(statistics.fmean(ok), 1) if ok else None,
        }


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    r = await client.post(
        "/auth/login", json={"email": email, "password": password, "token_in_body": True}
    )
    r.raise_for_status()
    token = r.json()["token"]
    client.cookies.clear()
    return token


async def _closed_loop(
    client: httpx.AsyncClient,
    make_request,  # noqa: ANN001 - async callable(client) -> httpx.Response
    *,
    concurrency: int,
    warmup_s: float,
    duration_s: float,
) -> Sample:
    sample = Sample()
    measuring = asyncio.Event()
    stop_at = time.perf_counter() + warmup_s + duration_s

    async def user() -> None:
        while time.perf_counter() < stop_at:
            t0 = time.perf_counter()
            try:
                r = await make_request(client)
                code = r.status_code
            except httpx.HTTPError as exc:
                code = type(exc).__name__
            dt = (time.perf_counter() - t0) * 1000
            if not measuring.is_set():
                continue
            if code == 200 or code == 201:
                sample.latencies_ms.append(dt)
            else:
                sample.errors[str(code)] = sample.errors.get(str(code), 0) + 1

    async def clock() -> None:
        await asyncio.sleep(warmup_s)
        sample.started = time.perf_counter()
        measuring.set()

    await asyncio.gather(clock(), *(user() for _ in range(concurrency)))
    sample.finished = time.perf_counter()
    return sample


async def _setup(client: httpx.AsyncClient, seed: dict, headers: dict[str, str]) -> dict:
    """Per-run fixtures for scenarios that need server-chosen ids: one
    `relates_to` edge (for evidence) and one conversation (for agent turns)."""
    r = await client.get(
        f"/graph/entities/{seed['entities']['drawing_extractor']}/neighborhood",
        headers=headers,
    )
    r.raise_for_status()
    edge = next(x for x in r.json()["edges"] if x["kind"] == "relates_to")
    conv = await client.post("/conversations", json={"title": "load test"}, headers=headers)
    conv.raise_for_status()
    return {
        "evidence": {
            "source_entity_id": edge["source"],
            "target_entity_id": edge["target"],
            "predicate": edge["predicate"],
        },
        "conversation_id": conv.json()["id"],
    }


def _requests(seed: dict, headers: dict[str, str], fixtures: dict) -> dict[str, object]:
    m = seed["memories"]
    e = seed["entities"]

    def get(path: str, **params: object):  # noqa: ANN202
        return lambda c: c.get(path, params=params or None, headers=headers)

    return {
        "auth_me": get("/auth/me"),
        "memory_search": get("/memories/search", limit=25),
        "memory_detail": get(f"/memories/{m['paddleocr']}"),
        "provenance": get(f"/memories/{m['paddleocr']}/provenance"),
        "graph_overview": get("/graph/overview"),
        "graph_neighborhood": get(f"/graph/entities/{e['drawing_extractor']}/neighborhood", hops=2),
        "documents": get("/documents"),
        "hybrid_search": lambda c: c.post(
            "/search", json={"query": "OCR", "limit": 50}, headers=headers
        ),
        "edge_evidence": get("/graph/edges/evidence", **fixtures["evidence"]),
        # With LLM_PROVIDER=fake this measures the agent loop, tools and
        # storage around an offline answer -- never a real model's latency.
        "agent_turn": lambda c: c.post(
            f"/conversations/{fixtures['conversation_id']}/agent-turn",
            json={"message": "What OCR library does Drawing Extractor use now?"},
            headers=headers,
        ),
    }


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--base-url", required=True)
    p.add_argument("--seed-file", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--concurrency", default="1,10,25", help="comma-separated levels")
    p.add_argument("--duration", type=float, default=8.0)
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument(
        "--scenarios",
        default="auth_me,memory_search,memory_detail,provenance,graph_overview,"
        "graph_neighborhood,documents,hybrid_search,edge_evidence,agent_turn",
    )
    p.add_argument("--login-levels", default="1,5,10")
    p.add_argument("--login-requests", type=int, default=20)
    p.add_argument("--label", default="")
    args = p.parse_args()

    seed = json.loads(args.seed_file.read_text())
    levels = [int(x) for x in args.concurrency.split(",") if x]
    scenarios = [s for s in args.scenarios.split(",") if s]
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    results: dict[str, object] = {"label": args.label, "base_url": args.base_url, "runs": {}}

    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        token = await _login(client, seed["a"]["email"], seed["password"])
        headers = {"Authorization": f"Bearer {token}"}
        reqs = _requests(seed, headers, await _setup(client, seed, headers))
        for name in scenarios:
            if name == "login":
                per_level = {}
                for level in [int(x) for x in args.login_levels.split(",") if x]:
                    sample = Sample(started=time.perf_counter())
                    sem = asyncio.Semaphore(level)

                    async def one(sample: Sample = sample, sem: asyncio.Semaphore = sem) -> None:
                        async with sem:
                            t0 = time.perf_counter()
                            r = await client.post(
                                "/auth/login",
                                json={
                                    "email": seed["a"]["email"],
                                    "password": seed["password"],
                                    "token_in_body": True,
                                },
                            )
                            client.cookies.clear()
                            if r.status_code == 200:
                                sample.latencies_ms.append((time.perf_counter() - t0) * 1000)
                            else:
                                key = str(r.status_code)
                                sample.errors[key] = sample.errors.get(key, 0) + 1

                    await asyncio.gather(*(one() for _ in range(args.login_requests)))
                    sample.finished = time.perf_counter()
                    per_level[str(level)] = sample.summary()
                results["runs"]["login"] = per_level
                continue
            per_level = {}
            for level in levels:
                sample = await _closed_loop(
                    client,
                    reqs[name],
                    concurrency=level,
                    warmup_s=args.warmup,
                    duration_s=args.duration,
                )
                per_level[str(level)] = sample.summary()
                print(f"{name:<20} c={level:<3} {per_level[str(level)]}", file=sys.stderr)
            results["runs"][name] = per_level

    args.out.write_text(json.dumps(results, indent=2))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
