"""The Phase 5 agent loop (docs/PHASE5_PLAN.md sections 5-9).

Reuses `LLMClient.complete_structured()` -- the exact same provider-agnostic
primitive `ExtractionRunner._extract` already uses, repair-prompt pattern
included -- for tool selection via `AgentStep`. No second LLM abstraction, no
native function-calling integration, no new dependency.

## Loop shape

    for iteration in 1..AGENT_MAX_TOOL_CALLS:
        ask the model for one AgentStep (one repair retry on malformed JSON)
        if final_answer:
            validate cited_memory_ids against every id any tool call this
              turn actually returned (one repair retry, then strip invalid
              citations rather than accept them) -> return
        if tool_call:
            if identical (tool_name, tool_args) already called this turn:
                stop, forced answer (loop detected)
            dispatch the tool (bounded timeout, never raises -- see
              app.agent.tools.dispatch_tool)
            record the trace, continue
    -> budget exhausted: forced answer from whatever was retrieved

The whole loop is wrapped in one `asyncio.wait_for` against
`AGENT_TURN_TIMEOUT_SECONDS`, so a stuck LLM call or a tool that itself hangs
past its own timeout cannot make a turn run forever. `run_turn` never raises
for a normal operational condition (malformed output, timeout, exhausted
budget) -- it always returns an `AgentTurnResult`, mirroring
`ExtractionRunner.run`'s own "never raises" contract. It DOES raise
`ConfigurationError` if the agent is disabled (`AGENT_ENABLED=false`), since
that is a caller/deployment mistake, not a normal turn outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.agent.prompts import build_agent_prompt, citation_repair_note, malformed_repair_note
from app.agent.tools import ToolDependencies, ToolResult, dispatch_tool
from app.core.config import Settings
from app.core.errors import ConfigurationError, LLMResponseInvalidError
from app.llm.base import LLMClient
from app.models.agent import AgentStep, FinalAnswerStep, ToolCallStep

logger = logging.getLogger(__name__)

#: Every outcome a turn can end in -- distinct values so observability and
#: evaluation can tell "the model answered normally" apart from every kind of
#: forced/degraded termination without parsing free text.
TerminationReason = str  # one of the _TERMINATION_* constants below
_TERMINATION_FINAL_ANSWER = "final_answer"
_TERMINATION_CITATION_STRIPPED = "citation_stripped"
_TERMINATION_BUDGET_EXHAUSTED = "budget_exhausted"
_TERMINATION_LOOP_DETECTED = "loop_detected"
_TERMINATION_TURN_TIMEOUT = "turn_timeout"
_TERMINATION_MALFORMED_OUTPUT = "malformed_output"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ToolTraceEntry:
    """One tool call's observability record (PHASE5_PLAN.md section 12) --
    never logs secrets; `tool_args` are the model's own (already-validated,
    bounded) arguments, not raw request data.
    """

    iteration: int
    tool_name: str
    tool_args: dict
    status: str
    latency_ms: float
    memory_ids_returned: list[str]


@dataclass(frozen=True)
class AgentTurnResult:
    answer: str
    confidence: str
    cited_memory_ids: list[str]
    tool_call_count: int
    termination_reason: str
    trace: list[ToolTraceEntry] = field(default_factory=list)
    #: Summed across every `complete_structured` call this turn actually made
    #: (including malformed/citation repair retries) -- 0 for a turn that
    #: timed out before any call completed, since a cancelled in-flight call
    #: never reports usage (CLAUDE.md section 29/32: "token usage... where
    #: measurable").
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _TokenUsage:
    """Mutable per-turn accumulator, threaded through `_run_loop`/`_finalize`
    as a parameter -- never stored on `self`, since `AgentService` is a
    long-lived, potentially-concurrent object and turn state must not leak
    across turns.
    """

    input_tokens: int = 0
    output_tokens: int = 0


def _canonical_args_key(tool_name: str, args: dict) -> str:
    """A stable string identifying one (tool_name, tool_args) pair, for loop
    detection -- `sort_keys=True` so argument order never matters, `default=str`
    so a non-JSON-native value (e.g. an accidental datetime) never raises here.
    """
    return f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"


def _parse_tool_args(raw: str) -> dict | None:
    """`ToolCallStep.tool_args_json` is a JSON-encoded object (see that
    field's docstring for why it is a string, not a nested object) -- this
    is the one place it is decoded back into a dict. Returns `None` for
    invalid JSON or a valid-JSON-but-non-object value (e.g. a bare list or
    number), never raises: the caller treats `None` exactly like a malformed
    structured response.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class AgentService:
    def __init__(
        self,
        *,
        llm_client: LLMClient,
        settings: Settings,
        now_fn: Callable[[], datetime] = _utcnow,
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._llm = llm_client
        self._settings = settings
        self._now = now_fn
        self._new_id = id_fn

    async def run_turn(
        self,
        *,
        user_id: str,
        conversation_window_text: str,
        user_message: str,
        deps: ToolDependencies,
    ) -> AgentTurnResult:
        """Structured observability (PHASE5_PLAN.md section 12): one log line
        at turn start and one at turn end, plus one per tool call (in
        `_run_loop`). Never logs message/memory *content* -- only lengths,
        ids, statuses and counts, matching the existing codebase's
        "do not unnecessarily log sensitive user content" discipline
        (CLAUDE.md section 32).
        """
        if not self._settings.agent_enabled:
            raise ConfigurationError("The agent is disabled (AGENT_ENABLED=false).")

        turn_id = self._new_id()
        start = time.perf_counter()
        logger.info(
            "agent_turn_start turn_id=%s user_id=%s message_chars=%d",
            turn_id,
            user_id,
            len(user_message),
        )
        try:
            result = await asyncio.wait_for(
                self._run_loop(
                    user_id=user_id,
                    conversation_window_text=conversation_window_text,
                    user_message=user_message,
                    deps=deps,
                ),
                timeout=self._settings.agent_turn_timeout_seconds,
            )
        except TimeoutError:
            result = AgentTurnResult(
                answer="This is taking longer than expected -- please try again.",
                confidence="not_found",
                cited_memory_ids=[],
                tool_call_count=0,
                termination_reason=_TERMINATION_TURN_TIMEOUT,
            )
        logger.info(
            "agent_turn_end turn_id=%s user_id=%s termination_reason=%s "
            "tool_call_count=%d cited_memory_count=%d latency_ms=%.1f",
            turn_id,
            user_id,
            result.termination_reason,
            result.tool_call_count,
            len(result.cited_memory_ids),
            (time.perf_counter() - start) * 1000,
        )
        return result

    async def _run_loop(
        self,
        *,
        user_id: str,
        conversation_window_text: str,
        user_message: str,
        deps: ToolDependencies,
    ) -> AgentTurnResult:
        tool_call_log: list[tuple[str, dict, ToolResult]] = []
        seen_memory_ids: set[str] = set()
        seen_calls: set[str] = set()
        trace: list[ToolTraceEntry] = []

        usage = _TokenUsage()
        for iteration in range(1, self._settings.agent_max_tool_calls + 1):
            prompt = build_agent_prompt(
                conversation_window_text=conversation_window_text,
                user_message=user_message,
                tool_call_log=tool_call_log,
            )
            step = await self._get_step_with_repair(prompt, usage)
            tool_args = (
                _parse_tool_args(step.tool_args_json) if isinstance(step, ToolCallStep) else None
            )
            if isinstance(step, ToolCallStep) and tool_args is None:
                # Schema-valid step, but tool_args_json itself was not valid
                # JSON -- treated exactly like a malformed structured
                # response: one repair attempt naming the actual problem,
                # then give up rather than dispatch a tool with garbage args.
                repair_prompt = (
                    prompt
                    + "\n"
                    + malformed_repair_note(
                        f"tool_args_json must be a JSON object string; got {step.tool_args_json!r}"
                    )
                )
                step = await self._get_step_with_repair(repair_prompt, usage)
                tool_args = (
                    _parse_tool_args(step.tool_args_json)
                    if isinstance(step, ToolCallStep)
                    else None
                )
                if isinstance(step, ToolCallStep) and tool_args is None:
                    step = None

            if step is None:
                return self._forced_answer(
                    tool_call_log, trace, usage, reason=_TERMINATION_MALFORMED_OUTPUT
                )

            if isinstance(step, FinalAnswerStep):
                return await self._finalize(
                    step,
                    prompt=prompt,
                    seen_memory_ids=seen_memory_ids,
                    tool_call_log=tool_call_log,
                    trace=trace,
                    usage=usage,
                )

            assert isinstance(step, ToolCallStep)
            assert tool_args is not None  # guaranteed by the repair handling above
            key = _canonical_args_key(step.tool_name, tool_args)
            if key in seen_calls:
                return self._forced_answer(
                    tool_call_log, trace, usage, reason=_TERMINATION_LOOP_DETECTED
                )
            seen_calls.add(key)

            start = time.perf_counter()
            result = await dispatch_tool(
                step.tool_name,
                tool_args,
                user_id=user_id,
                deps=deps,
                timeout=self._settings.agent_tool_timeout_seconds,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            tool_call_log.append((step.tool_name, tool_args, result))
            seen_memory_ids.update(result.memory_ids)
            trace.append(
                ToolTraceEntry(
                    iteration=iteration,
                    tool_name=step.tool_name,
                    tool_args=tool_args,
                    status=result.status.value,
                    latency_ms=latency_ms,
                    memory_ids_returned=result.memory_ids,
                )
            )
            logger.info(
                "agent_tool_call user_id=%s iteration=%d tool_name=%s tool_args=%s "
                "status=%s latency_ms=%.1f memory_ids_returned=%d",
                user_id,
                iteration,
                step.tool_name,
                json.dumps(tool_args, sort_keys=True, default=str),
                result.status.value,
                latency_ms,
                len(result.memory_ids),
            )

        return self._forced_answer(
            tool_call_log, trace, usage, reason=_TERMINATION_BUDGET_EXHAUSTED
        )

    async def _get_step_with_repair(
        self, prompt: str, usage: _TokenUsage
    ) -> ToolCallStep | FinalAnswerStep | None:
        """One repair-prompt retry on malformed structured output -- the
        exact same pattern `ExtractionRunner._extract` already uses. Returns
        `None` (never raises) if the repair attempt also fails, so the loop
        can degrade gracefully instead of the whole turn crashing. `usage` is
        incremented in place for every `complete_structured` call actually
        made (the repair attempt is a second real call and a second real
        cost, so it counts too).
        """
        try:
            result = await self._llm.complete_structured(
                prompt=prompt,
                schema=AgentStep,
                temperature=0.0,
                timeout=self._settings.agent_tool_timeout_seconds,
            )
            usage.input_tokens += result.input_tokens
            usage.output_tokens += result.output_tokens
            return result.value.step
        except LLMResponseInvalidError as exc:
            repair_prompt = prompt + "\n" + malformed_repair_note(exc.message)
            try:
                result = await self._llm.complete_structured(
                    prompt=repair_prompt,
                    schema=AgentStep,
                    temperature=0.0,
                    timeout=self._settings.agent_tool_timeout_seconds,
                )
                usage.input_tokens += result.input_tokens
                usage.output_tokens += result.output_tokens
                return result.value.step
            except LLMResponseInvalidError:
                return None
        # LLMUnavailableError is deliberately NOT caught here: PHASE5_PLAN.md
        # section 18 requires "the whole turn fails, return a controlled
        # error" for provider unavailability, unlike a malformed response --
        # it propagates out of run_turn to the API layer's existing error
        # taxonomy (the same mapping ExtractionRunner's LLMUnavailableError
        # handling already relies on), rather than degrading into a chat
        # answer that would misrepresent an outage as "nothing found".

    async def _finalize(
        self,
        step: FinalAnswerStep,
        *,
        prompt: str,
        seen_memory_ids: set[str],
        tool_call_log: list[tuple[str, dict, ToolResult]],
        trace: list[ToolTraceEntry],
        usage: _TokenUsage,
    ) -> AgentTurnResult:
        """Validates `cited_memory_ids` against every memory id any tool call
        actually returned this turn (PHASE5_PLAN.md section 9). An invalid
        citation triggers one repair-prompt retry naming exactly which ids
        were wrong; if the model still cites something invalid, the invalid
        ids are stripped (never accepted) and the answer is returned anyway,
        marked as a degraded termination -- a citation problem should not
        block the user from getting an answer, but it must never look like a
        clean citation either.
        """
        invalid = [m for m in step.cited_memory_ids if m not in seen_memory_ids]
        if not invalid:
            return AgentTurnResult(
                answer=step.answer,
                confidence=step.confidence,
                cited_memory_ids=step.cited_memory_ids,
                tool_call_count=len(tool_call_log),
                termination_reason=_TERMINATION_FINAL_ANSWER,
                trace=trace,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

        repair_prompt = prompt + "\n" + citation_repair_note(invalid)
        retried = await self._get_step_with_repair(repair_prompt, usage)
        if isinstance(retried, FinalAnswerStep):
            retried_invalid = [m for m in retried.cited_memory_ids if m not in seen_memory_ids]
            if not retried_invalid:
                return AgentTurnResult(
                    answer=retried.answer,
                    confidence=retried.confidence,
                    cited_memory_ids=retried.cited_memory_ids,
                    tool_call_count=len(tool_call_log),
                    termination_reason=_TERMINATION_FINAL_ANSWER,
                    trace=trace,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
            step = retried  # fall through to stripping below, using the retry's answer text

        cleaned = [m for m in step.cited_memory_ids if m in seen_memory_ids]
        return AgentTurnResult(
            answer=step.answer,
            confidence=step.confidence,
            cited_memory_ids=cleaned,
            tool_call_count=len(tool_call_log),
            termination_reason=_TERMINATION_CITATION_STRIPPED,
            trace=trace,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    def _forced_answer(
        self,
        tool_call_log: list[tuple[str, dict, ToolResult]],
        trace: list[ToolTraceEntry],
        usage: _TokenUsage,
        *,
        reason: str,
    ) -> AgentTurnResult:
        """A controlled, honest response when the loop cannot reach a normal
        final answer -- never a fabricated claim, never a silent repeat.
        """
        found_any = any(r.memory_ids for _, _, r in tool_call_log)
        if reason == _TERMINATION_LOOP_DETECTED:
            answer = (
                "I already checked that and have not found anything further to add."
                if found_any
                else "I was not able to find relevant information after checking."
            )
        elif reason == _TERMINATION_BUDGET_EXHAUSTED:
            answer = (
                "I looked into this but used up my available tool calls before "
                "reaching a complete answer. Here is what I found so far."
                if found_any
                else "I was not able to find relevant information within my tool-call budget."
            )
        else:  # malformed_output
            answer = "I ran into a problem forming a response. Please try rephrasing your request."

        all_memory_ids = sorted({mid for _, _, r in tool_call_log for mid in r.memory_ids})
        return AgentTurnResult(
            answer=answer,
            confidence="not_found" if not found_any else "low",
            cited_memory_ids=all_memory_ids,
            tool_call_count=len(tool_call_log),
            termination_reason=reason,
            trace=trace,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
