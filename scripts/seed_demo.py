#!/usr/bin/env python
"""Seed the deterministic demo dataset.

The narrative is the one used throughout the documentation, and it is the smallest
dataset that exercises temporal memory end to end:

    January   Drawing Extractor --uses--> EasyOCR
              Drawing Extractor --uses--> Gemini
              EasyOCR --related_to--> OCR        (gives the graph a second hop)
    May       Drawing Extractor --uses--> PaddleOCR, superseding the EasyOCR memory

Afterwards the EasyOCR fact is still present, with status `superseded` and a closed
validity window -- it is NOT deleted. That is the property the whole temporal model
exists to provide, so the script asserts it before reporting success.

    cd backend && uv run python ../scripts/seed_demo.py
    cd backend && uv run python ../scripts/seed_demo.py --reset   # wipe first

`--reset` removes only the seed user's nodes. It is a maintenance operation that
deliberately lives in a script rather than the API, because the API is append-only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.models.enums import MemoryStatus  # noqa: E402
from app.repositories.entity_repository import EntityRepository  # noqa: E402
from app.repositories.memory_repository import MemoryRepository  # noqa: E402
from app.schemas.memory import MemoryCreate  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402

SEED_USER_ID = "demo-user"

JANUARY = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
MAY = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
MARCH = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

#: Removes only this user's subgraph. Not exposed through the API by design.
RESET_QUERY = """
MATCH (n {user_id: $user_id})
DETACH DELETE n
RETURN count(n) AS removed
"""


class MutableClock:
    """Lets the seed write memories with historically accurate timestamps."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def memory(content: str, entities: list[tuple[str, str]], predicate: str,
           source_ref: str, **kw: object) -> MemoryCreate:
    src_name, tgt_name = entities[0][0], entities[1][0]
    data: dict[str, object] = {
        "content": content,
        "memory_type": "semantic",
        "entities": [{"name": n, "entity_type": t} for n, t in entities],
        "relationships": [
            {"source": src_name, "predicate": predicate, "target": tgt_name}
        ],
        "source": {"source_type": "conversation", "reference": source_ref},
    }
    data.update(kw)
    return MemoryCreate.model_validate(data)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the seed user's existing nodes before seeding",
    )
    parser.add_argument("--user-id", default=SEED_USER_ID)
    args = parser.parse_args()

    configure_logging("WARNING")
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

    user_id = args.user_id
    clock = MutableClock(JANUARY)
    memories = MemoryService(MemoryRepository(client), now_fn=clock)
    retrieval = RetrievalService(
        EntityRepository(client), max_hops=settings.graph_max_hops, now_fn=clock
    )

    try:
        if args.reset:
            rows = await client.write(RESET_QUERY, {"user_id": user_id})
            print(f"Reset: removed {rows[0]['removed'] if rows else 0} nodes\n")

        # --- January -----------------------------------------------------
        clock.now = JANUARY
        easyocr_memory = await memories.create(
            user_id=user_id,
            payload=memory(
                "The Drawing Extractor project uses EasyOCR for text extraction.",
                [("Drawing Extractor", "project"), ("EasyOCR", "technology")],
                "uses",
                "conversation-101",
            ),
        )
        print(f"January  created  {easyocr_memory.memory.content}")

        gemini_memory = await memories.create(
            user_id=user_id,
            payload=memory(
                "The Drawing Extractor project uses Gemini for reasoning.",
                [("Drawing Extractor", "project"), ("Gemini", "technology")],
                "uses",
                "conversation-101",
            ),
        )
        print(f"January  created  {gemini_memory.memory.content}")

        ocr_memory = await memories.create(
            user_id=user_id,
            payload=memory(
                "EasyOCR is an OCR library.",
                [("EasyOCR", "technology"), ("OCR", "concept")],
                "related_to",
                "conversation-102",
            ),
        )
        print(f"January  created  {ocr_memory.memory.content}")

        # --- May: the switch ---------------------------------------------
        clock.now = MAY
        paddle_memory = await memories.create(
            user_id=user_id,
            payload=memory(
                "The Drawing Extractor project switched from EasyOCR to PaddleOCR.",
                [("Drawing Extractor", "project"), ("PaddleOCR", "technology")],
                "uses",
                "conversation-205",
                supersedes=[easyocr_memory.memory.id],
            ),
        )
        print(f"May      created  {paddle_memory.memory.content}")
        print(f"May      superseded memory {easyocr_memory.memory.id}\n")

        # --- Verify history was preserved, not deleted -------------------
        old = await memories.get(user_id=user_id, memory_id=easyocr_memory.memory.id)
        assert old.memory.status is MemoryStatus.SUPERSEDED, old.memory.status
        assert old.memory.valid_until == MAY, old.memory.valid_until
        assert old.memory.content.startswith("The Drawing Extractor project uses EasyOCR")

        project = await retrieval.find_entity_by_name(
            user_id=user_id, name="Drawing Extractor"
        )

        now_state = await retrieval.get_entity_state(
            user_id=user_id, entity_id=project.id
        )
        march_state = await retrieval.get_entity_state(
            user_id=user_id, entity_id=project.id, as_of=MARCH
        )
        graph = await retrieval.get_neighbourhood(
            user_id=user_id, entity_id=project.id, hops=2
        )

        def names(views: list) -> list[str]:
            return sorted(v.target_entity.name for v in views)

        print("Drawing Extractor, as of now")
        print(f"  currently uses : {names(now_state.current)}")
        print(f"  previously used: {names(now_state.historical)}")
        print("\nDrawing Extractor, as of March")
        print(f"  currently uses : {names(march_state.current)}")
        print(f"  previously used: {names(march_state.historical)}")
        print("\n2-hop neighbourhood")
        for node in graph.nodes:
            print(f"  {node.hops} hop(s): {node.entity.name} ({node.entity.entity_type})")

        assert names(now_state.current) == ["Gemini", "PaddleOCR"], names(now_state.current)
        assert names(now_state.historical) == ["EasyOCR"], names(now_state.historical)
        assert names(march_state.current) == ["EasyOCR", "Gemini"]
        assert "OCR" in {n.entity.name for n in graph.nodes}

        print(
            "\nSeed complete. The EasyOCR memory is preserved as `superseded` "
            "with a closed validity window, not deleted."
        )
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
