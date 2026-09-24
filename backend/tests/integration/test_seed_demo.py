"""The seed dataset, verified end to end against real Neo4j.

This is the acceptance test for Phase 1: after the documented narrative is written,
the EasyOCR relationship must still exist as history rather than having been deleted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.graph.connection import GraphClient
from app.models.enums import MemoryStatus
from app.repositories.entity_repository import EntityRepository
from app.repositories.memory_repository import MemoryRepository
from app.services.memory_service import MemoryService
from app.services.retrieval_service import RetrievalService

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

pytestmark = pytest.mark.integration


@pytest.fixture
def seed_module():  # type: ignore[no-untyped-def]
    import seed_demo

    return seed_demo


@pytest.fixture
async def seeded(
    seed_module, graph: GraphClient, user_id: str, cleanup: None
):  # type: ignore[no-untyped-def]
    """Write the seed narrative using the script's own definitions."""
    clock = seed_module.MutableClock(seed_module.JANUARY)
    memories = MemoryService(MemoryRepository(graph), now_fn=clock)
    retrieval = RetrievalService(EntityRepository(graph), max_hops=4, now_fn=clock)

    clock.now = seed_module.JANUARY
    easy = await memories.create(
        user_id=user_id,
        payload=seed_module.memory(
            "The Drawing Extractor project uses EasyOCR for text extraction.",
            [("Drawing Extractor", "project"), ("EasyOCR", "technology")],
            "uses",
            "conversation-101",
        ),
    )
    await memories.create(
        user_id=user_id,
        payload=seed_module.memory(
            "The Drawing Extractor project uses Gemini for reasoning.",
            [("Drawing Extractor", "project"), ("Gemini", "technology")],
            "uses",
            "conversation-101",
        ),
    )
    await memories.create(
        user_id=user_id,
        payload=seed_module.memory(
            "EasyOCR is an OCR library.",
            [("EasyOCR", "technology"), ("OCR", "concept")],
            "related_to",
            "conversation-102",
        ),
    )
    clock.now = seed_module.MAY
    await memories.create(
        user_id=user_id,
        payload=seed_module.memory(
            "The Drawing Extractor project switched from EasyOCR to PaddleOCR.",
            [("Drawing Extractor", "project"), ("PaddleOCR", "technology")],
            "uses",
            "conversation-205",
            supersedes=[easy.memory.id],
        ),
    )
    return {
        "memories": memories,
        "retrieval": retrieval,
        "easyocr_memory_id": easy.memory.id,
        "clock": clock,
    }


async def test_easyocr_is_preserved_not_deleted(seeded, user_id: str) -> None:  # type: ignore[no-untyped-def]
    """The single most important property of the whole phase."""
    old = await seeded["memories"].get(
        user_id=user_id, memory_id=seeded["easyocr_memory_id"]
    )
    assert old.memory.status is MemoryStatus.SUPERSEDED
    assert "EasyOCR" in old.memory.content
    assert old.memory.valid_until is not None


async def test_current_and_previous_technologies(seeded, user_id: str, seed_module) -> None:  # type: ignore[no-untyped-def]
    project = await seeded["retrieval"].find_entity_by_name(
        user_id=user_id, name="Drawing Extractor"
    )
    state = await seeded["retrieval"].get_entity_state(
        user_id=user_id, entity_id=project.id
    )
    assert sorted(v.target_entity.name for v in state.current) == ["Gemini", "PaddleOCR"]
    assert sorted(v.target_entity.name for v in state.historical) == ["EasyOCR"]


async def test_state_in_march_returns_easyocr(seeded, user_id: str, seed_module) -> None:  # type: ignore[no-untyped-def]
    project = await seeded["retrieval"].find_entity_by_name(
        user_id=user_id, name="Drawing Extractor"
    )
    state = await seeded["retrieval"].get_entity_state(
        user_id=user_id, entity_id=project.id, as_of=seed_module.MARCH
    )
    assert sorted(v.target_entity.name for v in state.current) == ["EasyOCR", "Gemini"]


async def test_two_hop_reaches_ocr_concept(seeded, user_id: str) -> None:  # type: ignore[no-untyped-def]
    project = await seeded["retrieval"].find_entity_by_name(
        user_id=user_id, name="Drawing Extractor"
    )
    result = await seeded["retrieval"].get_neighbourhood(
        user_id=user_id, entity_id=project.id, hops=2
    )
    assert {n.entity.name for n in result.nodes} == {
        "EasyOCR", "Gemini", "PaddleOCR", "OCR",
    }


async def test_provenance_is_recorded(seeded, user_id: str) -> None:  # type: ignore[no-untyped-def]
    old = await seeded["memories"].get(
        user_id=user_id, memory_id=seeded["easyocr_memory_id"]
    )
    assert len(old.sources) == 1
    assert old.sources[0].reference == "conversation-101"
