"""Anti-hallucination grounding (docs/adr/0007). Pure function, no I/O.

Covers both grounding scopes deliberately: `source_span` strictly against the
target message, entity mentions against the whole window when no `mention_span`
is given -- the fix that makes pronoun-resolved facts ("we switched...") groundable
without also accepting genuinely fabricated entities.
"""

from __future__ import annotations

from app.extraction.grounding import check_grounding
from app.models.extraction import CandidateEntity, CandidateMemory, Span


def _span(content: str, needle: str) -> Span:
    start = content.index(needle)
    return Span(start=start, end=start + len(needle))


def _candidate(
    *,
    entities: list[CandidateEntity],
    source_span: Span,
    statement: str = "statement",
) -> CandidateMemory:
    return CandidateMemory(
        statement=statement,
        memory_type="semantic",
        persistence="persistent",
        confidence=0.9,
        entities=entities,
        relationships=[],
        source_message_id="m1",
        source_span=source_span,
        reasoning="r",
    )


class TestSourceSpan:
    def test_span_within_bounds_is_accepted(self) -> None:
        content = "The project uses EasyOCR for extraction."
        candidate = _candidate(entities=[], source_span=Span(start=0, end=len(content)))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is True

    def test_span_extending_past_the_message_is_rejected(self) -> None:
        content = "Short message."
        candidate = _candidate(entities=[], source_span=Span(start=0, end=9999))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is False
        assert "outside the target message" in result.reason

    def test_span_is_checked_against_the_target_message_only_never_the_window(self) -> None:
        """A span valid in an earlier window message but not in the target message
        must still be rejected -- the fact must be ASSERTED in the target message.
        """
        target = "Short."
        window = "This is a very long earlier message in the window, much longer."
        candidate = _candidate(entities=[], source_span=Span(start=0, end=40))
        result = check_grounding(candidate, target_message_content=target, window_text=window)
        assert result.grounded is False


class TestEntityMentionSpan:
    def test_entity_grounded_at_its_claimed_mention_span(self) -> None:
        content = "The project uses EasyOCR for extraction."
        entity = CandidateEntity(
            name="EasyOCR", entity_type="technology", mention_span=_span(content, "EasyOCR")
        )
        candidate = _candidate(entities=[entity], source_span=Span(start=0, end=len(content)))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is True

    def test_hallucinated_entity_at_a_wrong_span_is_rejected(self) -> None:
        content = "The project uses EasyOCR for extraction."
        entity = CandidateEntity(
            name="Gemini", entity_type="technology", mention_span=Span(start=17, end=24)
        )
        candidate = _candidate(entities=[entity], source_span=Span(start=0, end=len(content)))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is False
        assert "Gemini" in result.reason

    def test_mention_span_out_of_bounds_is_rejected(self) -> None:
        content = "Short."
        entity = CandidateEntity(
            name="EasyOCR", entity_type="technology", mention_span=Span(start=0, end=9999)
        )
        candidate = _candidate(entities=[entity], source_span=Span(start=0, end=len(content)))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is False

    def test_mention_span_is_checked_against_the_target_message_only(self) -> None:
        """Even with a window given, a *precise* mention_span claim must be
        honoured against the target message specifically -- a precise claim that
        turns out to be about a different message is still a wrong claim.
        """
        target = "We switched to PaddleOCR."
        window = "Earlier we used EasyOCR for everything."
        entity = CandidateEntity(
            name="EasyOCR", entity_type="technology", mention_span=Span(start=0, end=8)
        )
        candidate = _candidate(entities=[entity], source_span=Span(start=0, end=len(target)))
        result = check_grounding(candidate, target_message_content=target, window_text=window)
        assert result.grounded is False


class TestWindowWideFallback:
    """No mention_span given -- the entity must appear somewhere in the window.

    This is what makes Scenario C ("We switched from EasyOCR to PaddleOCR") work:
    "Drawing Extractor" is never named in that message, only established earlier.
    """

    def test_entity_established_earlier_in_the_window_is_grounded(self) -> None:
        target = "We switched from EasyOCR to PaddleOCR for better accuracy."
        window = "The Drawing Extractor project uses EasyOCR.\n\n" + target
        entities = [
            CandidateEntity(name="Drawing Extractor", entity_type="project"),
            CandidateEntity(
                name="PaddleOCR", entity_type="technology", mention_span=_span(target, "PaddleOCR")
            ),
        ]
        candidate = _candidate(entities=entities, source_span=Span(start=0, end=len(target)))
        result = check_grounding(candidate, target_message_content=target, window_text=window)
        assert result.grounded is True

    def test_entity_absent_from_the_entire_window_is_rejected(self) -> None:
        """A genuinely fabricated entity -- present nowhere the model could have
        read it -- is still caught even with the window-wide fallback.
        """
        target = "We switched from EasyOCR to PaddleOCR for better accuracy."
        window = target  # no earlier context at all
        entities = [CandidateEntity(name="Gemini", entity_type="technology")]
        candidate = _candidate(entities=entities, source_span=Span(start=0, end=len(target)))
        result = check_grounding(candidate, target_message_content=target, window_text=window)
        assert result.grounded is False
        assert "does not appear anywhere in the extraction window" in result.reason

    def test_no_window_given_falls_back_to_the_target_message_alone(self) -> None:
        content = "The project uses EasyOCR."
        entities = [CandidateEntity(name="Gemini", entity_type="technology")]
        candidate = _candidate(entities=entities, source_span=Span(start=0, end=len(content)))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is False

    def test_case_insensitive_match(self) -> None:
        target = "we switched to paddleocr today."
        window = "The team uses PaddleOCR now."
        entities = [
            CandidateEntity(
                name="PaddleOCR", entity_type="technology", mention_span=_span(target, "paddleocr")
            )
        ]
        candidate = _candidate(entities=entities, source_span=Span(start=0, end=len(target)))
        result = check_grounding(candidate, target_message_content=target, window_text=window)
        assert result.grounded is True


class TestNoEntities:
    def test_a_candidate_with_no_entities_is_grounded_by_span_alone(self) -> None:
        """Scenario E-like bare statements can have zero entities -- grounding
        must not require at least one.
        """
        content = "I prefer Java for coding, always have."
        candidate = _candidate(entities=[], source_span=Span(start=0, end=len(content)))
        result = check_grounding(candidate, target_message_content=content)
        assert result.grounded is True
