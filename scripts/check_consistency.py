"""Cross-store consistency check: PostgreSQL <-> Neo4j (Phase 11, M9).

    cd backend && uv run python ../scripts/check_consistency.py

Read-only. Reports references between the two stores that point at nothing
-- the expected residue of restoring PostgreSQL and Neo4j backups taken at
slightly different moments, or of a crash between a Neo4j write and its
PostgreSQL bookkeeping. Checks:

* memory_embeddings.memory_id           -> a Neo4j :Memory with that id+user
* extraction_candidates.memory_id       -> a Neo4j :Memory
* memory_review_candidates memory a/b   -> Neo4j :Memory nodes
* entity_merge_candidates entity a/b    -> Neo4j :Entity nodes
* Neo4j :Source (message) reference     -> a PostgreSQL message
* Neo4j :Source (document) reference    -> a PostgreSQL document

Exit status 1 when any orphan exists. Embeddings orphans are repairable
(delete the row; the backfill re-creates missing ones); orphaned sources are
provenance whose original has been lost, and are reported for a human.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import asyncpg  # noqa: E402
from neo4j import AsyncGraphDatabase  # noqa: E402

from app.core.config import get_settings  # noqa: E402

_PG_REFS = {
    "memory_embeddings": ("SELECT user_id, memory_id FROM memory_embeddings", "Memory"),
    "extraction_candidates": (
        "SELECT user_id, memory_id FROM extraction_candidates WHERE memory_id IS NOT NULL",
        "Memory",
    ),
    "memory_review_candidates.a": (
        "SELECT user_id, memory_id_a FROM memory_review_candidates",
        "Memory",
    ),
    "memory_review_candidates.b": (
        "SELECT user_id, memory_id_b FROM memory_review_candidates",
        "Memory",
    ),
    "entity_merge_candidates.a": (
        "SELECT user_id, entity_id_a FROM entity_merge_candidates",
        "Entity",
    ),
    "entity_merge_candidates.b": (
        "SELECT user_id, entity_id_b FROM entity_merge_candidates",
        "Entity",
    ),
}


async def check(*, database_url: str, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> dict:
    driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    try:
        async with driver.session() as s:
            graph: dict[str, set[tuple[str, str]]] = {}
            for label in ("Memory", "Entity"):
                rows = await (
                    await s.run(f"MATCH (n:{label}) RETURN n.user_id AS u, n.id AS i")
                ).data()
                graph[label] = {(r["u"], r["i"]) for r in rows}
            sources = await (await s.run(
                "MATCH (s:Source) RETURN s.user_id AS u, s.source_type AS t, s.reference AS r"
            )).data()  # fmt: skip
    finally:
        await driver.close()

    conn = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        orphans: dict[str, int] = {}
        for name, (sql, label) in _PG_REFS.items():
            rows = await conn.fetch(sql)
            orphans[f"pg.{name} -> neo4j.{label}"] = sum(
                1 for r in rows if (r[0], r[1]) not in graph[label]
            )
        messages = {(r[0], str(r[1])) for r in await conn.fetch("SELECT user_id, id FROM messages")}
        documents = {
            (r[0], str(r[1])) for r in await conn.fetch("SELECT user_id, id FROM documents")
        }
    finally:
        await conn.close()
    orphans["neo4j.Source(message) -> pg.messages"] = sum(
        1 for s in sources if s["t"] == "message" and (s["u"], s["r"]) not in messages
    )
    orphans["neo4j.Source(document) -> pg.documents"] = sum(
        1 for s in sources if s["t"] == "document" and (s["u"], s["r"]) not in documents
    )
    return {
        "neo4j": {
            "memories": len(graph["Memory"]),
            "entities": len(graph["Entity"]),
            "sources": len(sources),
        },
        "orphans": orphans,
        "orphans_total": sum(orphans.values()),
    }


def main() -> int:
    settings = get_settings()
    report = asyncio.run(
        check(
            database_url=settings.database_url,
            neo4j_uri=settings.neo4j_uri,
            neo4j_user=settings.neo4j_username,
            neo4j_password=settings.neo4j_password,
        )
    )
    print(json.dumps(report, indent=2))
    return 0 if report["orphans_total"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
