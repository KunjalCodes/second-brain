"""The memory-history walk, shared by the agent's `get_memory_history` tool
and `GET /graph/memories/{id}/timeline` (Phase 8, docs/PHASE8_PLAN.md
section 7.3): one implementation, not a copy.

Python-side assembly over existing single-memory reads (`MemoryService.get`),
no new Cypher. It is moved verbatim out of `app/agent/tools.py`; the tool's
output is regression-tested byte-for-byte against a frozen copy of the
pre-extraction code (tests/integration/test_memory_history_walk.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import NotFoundError
from app.models.domain import MemoryDetail
from app.services.memory_service import MemoryService


@dataclass(frozen=True)
class MemoryHistoryWalk:
    #: Visit order (the start memory first). Callers that present history
    #: chronologically sort it themselves.
    details: list[MemoryDetail]
    #: True iff the walk stopped at `max_chain` with linked memories unvisited.
    truncated: bool


def _links(detail: MemoryDetail, *, follow_conflicts: bool) -> set[str]:
    linked = {*detail.supersedes, *detail.superseded_by, *detail.duplicate_of, *detail.duplicates}
    if follow_conflicts:
        linked |= set(detail.conflicts_with)
    return linked


async def walk_memory_history(
    memory_service: MemoryService,
    *,
    user_id: str,
    memory_id: str,
    max_chain: int,
    follow_conflicts: bool = False,
) -> MemoryHistoryWalk:
    """Walk `supersedes`/`superseded_by`/`duplicate_of`/`duplicates` (and,
    only if `follow_conflicts`, `conflicts_with`) outward from one memory,
    visiting at most `max_chain` memories. A link to a memory that cannot be
    read for this user is skipped, never an error.

    Raises `NotFoundError` if the start memory does not exist for this user.
    """
    start = await memory_service.get(user_id=user_id, memory_id=memory_id)
    chain: dict[str, MemoryDetail] = {start.memory.id: start}
    to_visit = list(_links(start, follow_conflicts=follow_conflicts))
    while to_visit and len(chain) < max_chain:
        related_id = to_visit.pop()
        if related_id in chain:
            continue
        try:
            related = await memory_service.get(user_id=user_id, memory_id=related_id)
        except NotFoundError:
            continue
        chain[related_id] = related
        to_visit.extend(_links(related, follow_conflicts=follow_conflicts))

    truncated = any(related_id not in chain for related_id in to_visit)
    return MemoryHistoryWalk(details=list(chain.values()), truncated=truncated)
