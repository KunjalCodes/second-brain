"""Decide, synchronously and without ever calling the LLM, whether a just-stored
message should start or extend an extraction job (docs/adr/0008).

Runs inside the same transaction as storing the message
(`ConversationService.append_message`), so it is exactly as cheap and reliable as
the message write itself -- and, since it never touches the LLM or Neo4j, it never
adds latency the way a real extraction call would.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from app.core.config import Settings
from app.extraction.gate import evaluate_gate
from app.models.conversation import ExtractionStatus, Message
from app.repositories.extraction_repository import ExtractionJobRepository
from app.repositories.message_repository import MessageRepository


async def maybe_trigger_extraction(
    *,
    message: Message,
    message_repo: MessageRepository,
    job_repo: ExtractionJobRepository,
    settings: Settings,
    now: datetime,
    id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> None:
    """Gate a message and, if it passes, create or extend an extraction job.

    A skipped message is not a dead end: it is marked `skipped` with the gate's
    reason, and the periodic sweep re-evaluates it later under the wider window
    gate (docs/adr/0008's "gate mistake" reconciliation case) -- so a heuristic
    false negative here costs latency, never a permanently lost memory.
    """
    if not settings.extraction_enabled:
        return

    decision = evaluate_gate(message, min_chars=settings.extraction_min_message_chars)
    if not decision.passed:
        await message_repo.set_extraction_status(
            message_id=message.id,
            status=ExtractionStatus.SKIPPED,
            gate_reason=decision.reason,
        )
        return

    # One job per gated message (Phase 14). The earlier debounce retargeted a
    # still-pending job to the newest message; the prompt extracts only from
    # the TARGET, so every earlier message in a burst was silently never
    # extracted and stayed `queued` forever (docs/PHASE14_MEMORY_MODEL.md §0,
    # step 9). The cost is one LLM call per gated message -- the same as the
    # non-burst path always paid.
    window_start = max(0, message.sequence - settings.extraction_window_size + 1)
    await job_repo.create_job(
        job_id=id_fn(),
        conversation_id=message.conversation_id,
        target_message_id=message.id,
        window_start_sequence=window_start,
        window_end_sequence=message.sequence,
        extractor_version=settings.extractor_version,
        now=now,
    )

    await message_repo.set_extraction_status(
        message_id=message.id, status=ExtractionStatus.QUEUED
    )
