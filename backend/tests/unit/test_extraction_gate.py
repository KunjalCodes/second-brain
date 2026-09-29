"""The deterministic heuristic gate (docs/adr/0008).

Pure function, no I/O -- every case here runs with no database and no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.extraction.gate import evaluate_gate
from app.models.conversation import ExtractionStatus, Message, MessageRole

MIN_CHARS = 25


def _message(content: str, role: MessageRole = MessageRole.USER) -> Message:
    return Message(
        id="m1",
        conversation_id="c1",
        user_id="u1",
        role=role,
        content=content,
        sequence=0,
        created_at=datetime.now(UTC),
        extraction_status=ExtractionStatus.PENDING,
    )


class TestPassingCases:
    @pytest.mark.parametrize(
        "content",
        [
            "I am building a project called Drawing Extractor.",
            "The project uses Gemini for classification.",
            "We switched from EasyOCR to PaddleOCR.",
            "I prefer Java for coding.",  # Scenario E -- exactly at the edge
        ],
    )
    def test_declarative_statements_pass(self, content: str) -> None:
        decision = evaluate_gate(_message(content), min_chars=MIN_CHARS)
        assert decision.passed is True

    def test_exactly_at_the_minimum_length_passes(self) -> None:
        content = "x" * MIN_CHARS
        assert evaluate_gate(_message(content), min_chars=MIN_CHARS).passed is True


class TestFailingCases:
    def test_assistant_messages_are_never_gated_through(self) -> None:
        content = "This is a long enough assistant reply about the project setup."
        decision = evaluate_gate(_message(content, role=MessageRole.ASSISTANT), min_chars=MIN_CHARS)
        assert decision.passed is False
        assert "not a user message" in decision.reason

    def test_system_messages_are_never_gated_through(self) -> None:
        content = "This is a long enough system message about the project setup."
        decision = evaluate_gate(_message(content, role=MessageRole.SYSTEM), min_chars=MIN_CHARS)
        assert decision.passed is False

    def test_empty_message_fails(self) -> None:
        decision = evaluate_gate(_message(""), min_chars=MIN_CHARS)
        assert decision.passed is False
        assert "empty" in decision.reason

    def test_whitespace_only_message_fails(self) -> None:
        decision = evaluate_gate(_message("   \n\t  "), min_chars=MIN_CHARS)
        assert decision.passed is False

    def test_too_short_message_fails(self) -> None:
        decision = evaluate_gate(_message("I'm tired today."), min_chars=MIN_CHARS)
        assert decision.passed is False
        assert "character minimum" in decision.reason

    def test_one_character_under_the_minimum_fails(self) -> None:
        content = "x" * (MIN_CHARS - 1)
        assert evaluate_gate(_message(content), min_chars=MIN_CHARS).passed is False

    @pytest.mark.parametrize(
        "content",
        [
            "What was I using for OCR in March this year?",
            "How are these two concepts connected together?",
        ],
    )
    def test_bare_questions_fail(self, content: str) -> None:
        decision = evaluate_gate(_message(content), min_chars=MIN_CHARS)
        assert decision.passed is False
        assert "question" in decision.reason

    def test_a_question_alongside_a_declarative_sentence_still_passes(self) -> None:
        """Only a message that is ENTIRELY a question is treated as a bare question."""
        content = "We switched to PaddleOCR. Do you think that was the right call?"
        decision = evaluate_gate(_message(content), min_chars=MIN_CHARS)
        assert decision.passed is True

    @pytest.mark.parametrize(
        "content",
        ["ok", "Thanks!", "sounds good", "Got it, thanks.", "yep", "Sure thing"],
    )
    def test_filler_phrases_fail(self, content: str) -> None:
        # Pad some cases past the length minimum to isolate the filler check from
        # the length check.
        decision = evaluate_gate(_message(content), min_chars=1)
        assert decision.passed is False

    def test_filler_check_is_whole_message_only_not_substring(self) -> None:
        """"ok" embedded in a real sentence must not be treated as pure filler."""
        content = "ok, one more thing -- we also switched to PaddleOCR entirely."
        decision = evaluate_gate(_message(content), min_chars=MIN_CHARS)
        assert decision.passed is True


class TestDeterminism:
    def test_same_input_always_produces_the_same_decision(self) -> None:
        message = _message("The project uses Gemini for classification.")
        first = evaluate_gate(message, min_chars=MIN_CHARS)
        second = evaluate_gate(message, min_chars=MIN_CHARS)
        assert first == second

    def test_gate_never_calls_out_to_anything(self) -> None:
        """A structural guarantee: the gate module imports nothing that performs
        I/O. If this ever needs an LLM import, the gate has stopped being cheap
        and deterministic.
        """
        import ast
        import inspect

        import app.extraction.gate as gate_module

        tree = ast.parse(inspect.getsource(gate_module))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        forbidden = {"httpx", "requests", "neo4j", "asyncpg", "sqlalchemy", "google"}
        assert not (imported & forbidden)


class TestShortDeclaratives:
    """Phase 14 (M1): short durable first-person facts were skipped forever."""

    @pytest.mark.parametrize(
        "content",
        ["I use Postgres now.", "We switched to Rust.", "My team uses Linear.", "I lead Atlas."],
    )
    def test_short_durable_first_person_statements_pass(self, content: str) -> None:
        assert len(content) < MIN_CHARS
        assert evaluate_gate(_message(content), min_chars=MIN_CHARS).passed is True

    @pytest.mark.parametrize(
        "content",
        ["I'm tired today.", "Do I use Postgres?", "Use Postgres.", "I use it", "ok we use"],
    )
    def test_other_short_messages_are_still_skipped(self, content: str) -> None:
        decision = evaluate_gate(_message(content), min_chars=MIN_CHARS)
        assert decision.passed is False, content
