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

    window_start = max(0, message.sequence - settings.extraction_window_size + 1)
    pending = await job_repo.find_pending_job_for_conversation(
        conversation_id=message.conversation_id
    )
    if pending is not None:
        # Debounce: a burst of messages collapses into one extraction over an
        # extended window rather than one job per message.
        await job_repo.extend_job(
            job_id=pending.id,
            target_message_id=message.id,
            window_end_sequence=message.sequence,
        )
    else:
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
