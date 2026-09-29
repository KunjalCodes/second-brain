""" "Why does the system remember this?" (Phase 14, M17).

Composes stored records only -- see `app.schemas.explanation`. User scoping is
inherited: every read goes through a user-scoped repository or service.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.extraction import DecisionType
from app.repositories.extraction_repository import ExtractionCandidateRepository
from app.repositories.memory_review_repository import MemoryReviewCandidateRepository
from app.schemas.explanation import (
    ExtractionDecisionView,
    MemoryExplanation,
    ReviewItemView,
    parse_reason,
)
from app.services.memory_service import MemoryService
from app.services.provenance_service import ProvenanceService

_CORROBORATION = {DecisionType.DUPLICATE, DecisionType.UPDATE}


class MemoryExplanationService:
    def __init__(self, session: AsyncSession, memory_service: MemoryService) -> None:
        self._session = session
        self._memories = memory_service

    async def explain(self, *, user_id: str, memory_id: str) -> MemoryExplanation:
        detail = await self._memories.get(user_id=user_id, memory_id=memory_id)  # 404s
        provenance = ProvenanceService(self._session, self._memories)
        sources = await provenance.resolve_sources(user_id=user_id, sources=detail.sources)
        records = await ExtractionCandidateRepository(
            self._session, user_id=user_id
        ).list_for_memory(memory_id=memory_id)
        decisions = []
        for r in records:
            worth, temporal = parse_reason(r.decision_reason)
            hints = r.payload.get("temporal") or {}
            decisions.append(
                ExtractionDecisionView(
                    decision=r.decision.value,
                    reason=r.decision_reason,
                    worthiness_rule=worth,
                    temporal_interpretation=temporal,
                    role="corroboration" if r.decision in _CORROBORATION else "creation",
                    persistence=r.payload.get("persistence"),
                    model_confidence=r.confidence,
                    model_reasoning=r.payload.get("reasoning"),
                    temporal_hints={k: v for k, v in hints.items() if v not in (None, False)},
                    source_kind="message" if r.source_message_id else "document_chunk",
                    source_id=r.source_message_id or r.source_chunk_id or "",
                    recorded_at=r.created_at,
                )
            )
        reviews = await MemoryReviewCandidateRepository(
            self._session, user_id=user_id
        ).list_for_memory(memory_id=memory_id)
        return MemoryExplanation(
            memory=detail.memory,
            origin="extraction" if decisions else "direct",
            decisions=decisions,
            sources=sources,
            relationships=detail.relationships,
            supersedes=detail.supersedes,
            superseded_by=detail.superseded_by,
            conflicts_with=detail.conflicts_with,
            duplicate_of=detail.duplicate_of,
            review=[
                ReviewItemView(
                    id=v["id"],
                    kind=v["kind"],
                    status=v["status"],
                    other_memory_id=(
                        v["memory_id_b"] if v["memory_id_a"] == memory_id else v["memory_id_a"]
                    ),
                    score=v["score"],
                    created_at=v["created_at"],
                )
                for v in reviews
            ],
        )
