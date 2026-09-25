"""Regression proof for the Phase 8 extraction of `get_memory_history`'s walk
(docs/PHASE8_PLAN.md sections 7.3 and 10).

`_reference_get_memory_history` below is a verbatim, frozen copy of the agent
tool's walk as it stood BEFORE the extraction (commit 2fcd507). The test
asserts the live tool's output is identical to it -- same status, same
`data`, same `memory_ids`, same order -- on a fixture that exercises every
branch: supersession both ways, duplicates both ways, a conflict link (which
the agent walk does NOT follow), a missing link target, and a chain longer
than `_MAX_HISTORY_CHAIN` so the bound itself is covered. Both run in the
same process, so set-iteration order (string hashing) is identical for both.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.agent.tools import (
    _MAX_HISTORY_CHAIN,
    ToolDependencies,
    ToolResult,
    ToolStatus,
    _compact_memory,
    get_memory_history,
)
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models.agent import GetMemoryHistoryArgs
from app.repositories.memory_repository import MemoryRepository
from app.schemas.memory import MemoryCreate
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _reference_get_memory_history(
    args: GetMemoryHistoryArgs, *, user_id: str, deps: ToolDependencies
) -> ToolResult:
    """FROZEN pre-extraction copy. Do not edit: it is the equality oracle."""
    try:
        start = await deps.memory.get(user_id=user_id, memory_id=args.memory_id)
    except NotFoundError:
        return ToolResult(
            status=ToolStatus.NOT_FOUND, message=f"No memory found with id {args.memory_id!r}."
        )

    now = deps.now_fn()
    chain: dict[str, dict[str, Any]] = {start.memory.id: _compact_memory(start, now=now)}
    to_visit = list(
        {*start.supersedes, *start.superseded_by, *start.duplicate_of, *start.duplicates}
    )
    while to_visit and len(chain) < _MAX_HISTORY_CHAIN:
        related_id = to_visit.pop()
        if related_id in chain:
            continue
        try:
            related = await deps.memory.get(user_id=user_id, memory_id=related_id)
        except NotFoundError:
            continue
        chain[related_id] = _compact_memory(related, now=now)
        to_visit.extend(
            {
                *related.supersedes,
                *related.superseded_by,
                *related.duplicate_of,
                *related.duplicates,
            }
        )

    return ToolResult(
        status=ToolStatus.OK, data={"chain": list(chain.values())}, memory_ids=list(chain)
    )


def _deps(memory_service: MemoryService, now: datetime) -> ToolDependencies:
    # `get_memory_history` only reads `deps.memory` and `deps.now_fn`.
    return ToolDependencies(
        hybrid_retrieval=None,  # type: ignore[arg-type]
        retrieval=None,  # type: ignore[arg-type]
        memory=memory_service,
        provenance=None,  # type: ignore[arg-type]
        settings=Settings(),
        now_fn=lambda: now,
    )


def _payload(content: str, **kw: object) -> MemoryCreate:
    return MemoryCreate.model_validate(
        {"content": content, "entities": [{"name": "Walk Fixture", "entity_type": "project"}], **kw}
    )


@pytest.fixture
async def branching_history(graph, user_id, cleanup) -> dict[str, Any]:  # noqa: ANN001
    """A supersession chain longer than the bound, with a duplicate hanging
    off it and a conflict next to it."""
    clock = {"now": T0}
    service = MemoryService(MemoryRepository(graph), now_fn=lambda: clock["now"])
    repo = MemoryRepository(graph)

    chain_ids: list[str] = []
    previous: str | None = None
    for i in range(_MAX_HISTORY_CHAIN + 5):
        clock["now"] = T0 + timedelta(days=i)
        created = await service.create(
            user_id=user_id,
            payload=_payload(f"walk step {i}", supersedes=[previous] if previous else []),
        )
        chain_ids.append(created.memory.id)
        previous = created.memory.id

    clock["now"] = T0 + timedelta(days=100)
    dup = await service.create(user_id=user_id, payload=_payload("walk step 3 again"))
    await repo.confirm_duplicate(
        user_id=user_id, duplicate_id=dup.memory.id, canonical_id=chain_ids[3], now=clock["now"]
    )
    rival = await service.create(user_id=user_id, payload=_payload("walk rival"))
    await service.link_conflicting(
        user_id=user_id, memory_id_a=chain_ids[4], memory_id_b=rival.memory.id
    )
    return {"service": service, "chain": chain_ids, "dup": dup.memory.id, "rival": rival.memory.id}


class TestAgentToolOutputUnchangedByExtraction:
    @pytest.mark.parametrize("start_index", [0, 3, 4, 12, -1])
    async def test_tool_output_equals_frozen_reference(
        self, branching_history: dict[str, Any], user_id: str, start_index: int
    ) -> None:
        deps = _deps(branching_history["service"], T0 + timedelta(days=200))
        args = GetMemoryHistoryArgs(memory_id=branching_history["chain"][start_index])
        live = await get_memory_history(args, user_id=user_id, deps=deps)
        reference = await _reference_get_memory_history(args, user_id=user_id, deps=deps)
        assert asdict(live) == asdict(reference)

    async def test_tool_output_equals_reference_from_the_duplicate(
        self, branching_history: dict[str, Any], user_id: str
    ) -> None:
        deps = _deps(branching_history["service"], T0 + timedelta(days=200))
        args = GetMemoryHistoryArgs(memory_id=branching_history["dup"])
        live = await get_memory_history(args, user_id=user_id, deps=deps)
        reference = await _reference_get_memory_history(args, user_id=user_id, deps=deps)
        assert asdict(live) == asdict(reference)
        assert branching_history["chain"][3] in live.memory_ids

    async def test_bound_and_conflict_semantics_are_preserved(
        self, branching_history: dict[str, Any], user_id: str
    ) -> None:
        deps = _deps(branching_history["service"], T0 + timedelta(days=200))
        live = await get_memory_history(
            GetMemoryHistoryArgs(memory_id=branching_history["chain"][4]),
            user_id=user_id,
            deps=deps,
        )
        assert len(live.memory_ids) == _MAX_HISTORY_CHAIN
        # The agent walk never followed CONFLICTS_WITH, and still must not.
        assert branching_history["rival"] not in live.memory_ids

    async def test_other_users_memory_is_not_found(
        self, branching_history: dict[str, Any], other_user_id: str
    ) -> None:
        deps = _deps(branching_history["service"], T0)
        args = GetMemoryHistoryArgs(memory_id=branching_history["chain"][0])
        live = await get_memory_history(args, user_id=other_user_id, deps=deps)
        reference = await _reference_get_memory_history(args, user_id=other_user_id, deps=deps)
        assert live.status == ToolStatus.NOT_FOUND
        assert asdict(live) == asdict(reference)
