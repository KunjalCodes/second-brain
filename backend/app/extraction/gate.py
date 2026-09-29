"""The deterministic heuristic gate (docs/adr/0008).

Decides whether a message is worth an LLM call at all, before any extraction job is
created. This is cheap, synchronous, unit-tested, and runs in the request path
alongside storing the message -- it never calls a model.

The gate is explicitly allowed to produce **false positives** (letting through a
message that turns out to have nothing extractable -- caught later, cheaply, by the
persistence classification in the candidate itself) but should avoid **permanent**
false negatives: a message it skips is recorded with a reason
(`messages.extraction_gate_reason`) and re-evaluated later by the sweep under the
*window* gate, so a heuristic mistake never permanently costs a memory
(docs/adr/0008, "gate mistake" reconciliation case).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.conversation import Message, MessageRole

#: Whole-message filler that carries no extractable content, matched only when it
#: is the ENTIRE message (case- and punctuation-insensitive) -- never as a
#: substring, so "ok, and one more thing: we switched to PaddleOCR" is untouched.
_FILLER_PHRASES = frozenset(
    {
        "ok", "okay", "sure", "yes", "no", "yep", "nope", "thanks", "thank you",
        "got it", "sounds good", "cool", "nice", "great", "perfect", "makes sense",
        "understood", "alright", "fine", "good", "hi", "hello", "hey", "bye",
        "got it thanks", "sure thing", "no worries", "youre welcome",
        "thank you so much", "no problem", "will do", "sounds great",
    }
)

_PUNCTUATION = re.compile(r"[^\w\s]")

#: Phase 14 (M1): a short first-person declarative with a durable verb ("I use
#: Postgres now.", "We switched to Rust.") is exactly the kind of fact the
#: length floor used to drop for good -- the sweep's re-check runs this same
#: gate, so a skip here was permanent. Narrow on purpose: subject pronoun first,
#: a durable verb, at least three words, and not a question.
_SHORT_DECLARATIVE = re.compile(
    r"^\s*(?:i|we|my|our)\b.*\b(?:use|uses|prefer|work|works|live|switched|moved|decided|"
    r"chose|picked|run|runs|deploy|deploys|own|lead|manage)\b",
    re.IGNORECASE,
)
#: "I use it" names nothing -- not worth an LLM call.
_ENDS_WITH_PRONOUN = re.compile(r"\b(?:it|that|this|them|one|those|these)\W*$", re.IGNORECASE)


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reason: str


def _normalize_for_filler_check(content: str) -> str:
    return _PUNCTUATION.sub("", content).strip().lower()


def _is_bare_question(content: str) -> bool:
    """Whether the whole message reads as a single question with no declarative
    clause elsewhere -- extracting a "fact" from a pure query is nonsensical.
    """
    trimmed = content.strip()
    if not trimmed.endswith("?"):
        return False
    body = trimmed[:-1]
    # A '.' or '!' earlier in the message suggests a separate declarative
    # sentence exists alongside the question, so don't treat the whole thing as
    # a bare question.
    return "." not in body and "!" not in body


def evaluate_gate(message: Message, *, min_chars: int) -> GateDecision:
    """Pure function: the same message and threshold always produce the same
    decision, which is what makes the gate's own precision measurable against the
    evaluation dataset (EVALUATION.md section 7).
    """
    if message.role is not MessageRole.USER:
        return GateDecision(False, "not a user message")

    content = message.content.strip()
    if not content:
        return GateDecision(False, "empty message")

    if len(content) < min_chars and not (
        len(content.split()) >= 3
        and not content.endswith("?")
        and _SHORT_DECLARATIVE.search(content)
        and not _ENDS_WITH_PRONOUN.search(content)
    ):
        return GateDecision(
            False, f"shorter than the {min_chars}-character minimum"
        )

    if _is_bare_question(content):
        return GateDecision(False, "reads as a question with no declarative content")

    if _normalize_for_filler_check(content) in _FILLER_PHRASES:
        return GateDecision(False, "matches a known filler phrase")

    return GateDecision(True, "passed heuristic gate")
