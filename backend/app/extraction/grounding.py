"""Anti-hallucination grounding check (docs/adr/0007).

A candidate's `source_span` is not decoration -- it is verified against the actual
target message before anything downstream sees the candidate. A fact with no
textual anchor is rejected deterministically here, not argued with by a
prompt-engineering fix.

## Two different checks, deliberately at two different scopes

`source_span` -- where the FACT was asserted -- is checked strictly against the
**target message only**: the statement must actually have been made there.

Entity mentions are checked more permissively, against the **whole extraction
window**, and this is not a relaxation for convenience -- it is required for the
pipeline to work at all. "We switched from EasyOCR to PaddleOCR" names no project;
resolving "we" to "Drawing Extractor" is the entire reason extraction runs over a
*window* of messages rather than one message at a time (docs/adr/0008). An entity
established two turns earlier and only referenced pronominally in the target
message is not a hallucination -- checking it only against the target message's
text would reject Scenario C outright, exactly the scenario windowing exists to
handle.

The strict, offset-precise check still applies whenever the model *does* supply a
`mention_span`: a candidate that claims a specific location for a name is held to
that specific location, target-message-only. Only a name given with no
`mention_span` falls back to the window-wide "does this appear anywhere the model
could have read it" check -- looser, but still real: an entity naming something
that appears nowhere in the entire visible window is still rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.extraction import CandidateMemory, Span


@dataclass(frozen=True)
class GroundingResult:
    grounded: bool
    reason: str


def _span_text(content: str, span: Span) -> str | None:
    """The substring a span points to, or None if the span is out of bounds."""
    if span.start < 0 or span.end > len(content):
        return None
    return content[span.start : span.end]


def check_grounding(
    candidate: CandidateMemory,
    *,
    target_message_content: str,
    window_text: str | None = None,
) -> GroundingResult:
    """Verify a candidate's span and entity mentions against the real messages.

    `window_text` should be the concatenation of every message's content in the
    extraction window (any order that preserves substring matching is fine); it
    defaults to `target_message_content` alone, which is correct for a
    single-message window and for tests that don't need multi-message resolution.

    Returns a result rather than raising: an ungrounded candidate is a normal,
    expected classification (`DecisionType.INVALID`) applied by the caller, not an
    exceptional condition.
    """
    source_text = _span_text(target_message_content, candidate.source_span)
    if source_text is None:
        return GroundingResult(
            False,
            f"source_span {candidate.source_span.start}:{candidate.source_span.end} "
            f"is outside the target message (length {len(target_message_content)})",
        )

    search_text = (window_text if window_text is not None else target_message_content).lower()

    for entity in candidate.entities:
        if entity.mention_span is not None:
            # A precise claim is held to that precise, target-message-only location.
            text = _span_text(target_message_content, entity.mention_span)
            if text is None:
                return GroundingResult(
                    False,
                    f"entity {entity.name!r} mention_span is outside the target message",
                )
            if entity.name.strip().lower() not in text.lower():
                return GroundingResult(
                    False,
                    f"entity {entity.name!r} does not appear at its claimed mention_span",
                )
        elif entity.name.strip().lower() not in search_text:
            return GroundingResult(
                False,
                f"entity {entity.name!r} does not appear anywhere in the extraction window",
            )

    return GroundingResult(True, "source_span and every entity mention are grounded")
