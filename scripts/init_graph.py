#!/usr/bin/env python
"""Apply the Neo4j schema (constraints and indexes).

Run once against a new database, and again after any change to
`app/graph/schema.py`. Every statement is `IF NOT EXISTS`, so re-running is safe.

    cd backend && uv run python ../scripts/init_graph.py

Reads NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD from the environment (or ../.env),
so the same command targets a local instance or Neo4j Aura.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.graph.schema import (  # noqa: E402
    SCHEMA_STATEMENTS,
    SHOW_CONSTRAINTS,
    SHOW_INDEXES,
)


async def main() -> int:
    configure_logging("INFO")
    settings = get_settings()
    if not settings.neo4j_configured:
        print(
            "ERROR: Neo4j is not configured. Set NEO4J_URI, NEO4J_USERNAME and "
            "NEO4J_PASSWORD (see .env.example).",
            file=sys.stderr,
        )
        return 2

    client = GraphClient(settings)
    await client.connect()
    try:
        await client.verify()
    except Exception as exc:
        print(f"ERROR: cannot reach Neo4j: {exc}", file=sys.stderr)
        await client.close()
        return 2

    print(f"Applying {len(SCHEMA_STATEMENTS)} schema statements...\n")
    failures = 0
    for statement in SCHEMA_STATEMENTS:
        name = statement.split()[2]
        try:
            await client.write(statement)
            print(f"  ok       {name}")
        except Exception as exc:
            # Reported, never swallowed: a missing constraint silently weakens
            # entity resolution and concurrent-write safety.
            failures += 1
            print(f"  FAILED   {name}: {exc}", file=sys.stderr)

    print("\nConstraints now present:")
    for row in await client.read(SHOW_CONSTRAINTS):
        print(f"  {row['name']:30} {row['labelsOrTypes']} {row['properties']}")
    print("\nIndexes now present:")
    for row in await client.read(SHOW_INDEXES):
        if row.get("type") != "LOOKUP":
            print(f"  {row['name']:30} {row['labelsOrTypes']} {row['properties']}")

    await client.close()
    if failures:
        print(f"\n{failures} statement(s) failed.", file=sys.stderr)
        return 1
    print("\nSchema applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
