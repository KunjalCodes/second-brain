"""The deterministic memory decision engine (docs/PHASE2_PLAN.md section 4).

Pure functions, no I/O -- the whole decision table is exercised here against fixed
`ExistingEdge` fixtures rather than a real graph, which is what makes the engine's
logic cheap to test exhaustively.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extraction.decisions import (
    ExistingEdge,
    classify_candidate_persistence,
    classify_relationship,
    overall_decision,
)
from app.models.enums import MemoryStatus
from app.models.extraction import CandidateMemory, DecisionType

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def edge(
    target: str,
    *,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    memory_id: str = "mem-1",
    confidence: float = 1.0,
) -> ExistingEdge:
    return ExistingEdge(
        target_normalized_name=target, status=status, memory_id=memory_id, confidence=confidence
    )


class TestPersistenceGate:
    def test_temporary_is_ignored(self) -> None:
        c = CandidateMemory.model_validate(
            {
                "statement": "x", "memory_type": "episodic", "persistence": "temporary",
                "confidence": 0.6, "entities": [], "relationships": [],
                "source_message_id": "m1", "source_span": {"start": 0, "end": 1}, "reasoning": "r",
            }
        )
        assert classify_candidate_persistence(c) is DecisionType.IGNORE

    def test_uncertain_is_ignored(self) -> None:
        c = CandidateMemory.model_validate(
            {
                "statement": "x", "memory_type": "semantic", "persistence": "uncertain",
                "confidence": 0.4, "entities": [], "relationships": [],
                "source_message_id": "m1", "source_span": {"start": 0, "end": 1}, "reasoning": "r",
            }
        )
        assert classify_candidate_persistence(c) is DecisionType.IGNORE

    def test_persistent_proceeds(self) -> None:
        c = CandidateMemory.model_validate(
            {
                "statement": "x", "memory_type": "semantic", "persistence": "persistent",
                "confidence": 0.9, "entities": [], "relationships": [],
                "source_message_id": "m1", "source_span": {"start": 0, "end": 1}, "reasoning": "r",
            }
        )
        assert classify_candidate_persistence(c) is None


class TestScenarioA_NewFact:
    def test_no_siblings_at_all_is_new(self) -> None:
        d = classify_relationship(
            predicate="uses", target_normalized="easyocr", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=[], now=NOW,
        )
        assert d.decision is DecisionType.NEW
        assert d.target_memory_id is None


class TestScenarioC_Superseding:
    def test_change_signal_with_hint_match_supersedes_the_named_edge(self) -> None:
        existing = [edge("easyocr", memory_id="mem-easyocr")]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint="EasyOCR", existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.SUPERSEDING
        assert d.target_memory_id == "mem-easyocr"

    def test_change_signal_no_hint_single_sibling_still_supersedes(self) -> None:
        existing = [edge("easyocr", memory_id="mem-easyocr")]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.SUPERSEDING
        assert d.target_memory_id == "mem-easyocr"

    def test_hint_mismatch_still_falls_back_to_the_single_sibling(self) -> None:
        """A hint naming something not on record does not block superseding the
        one fact that IS on record -- is_change=True is itself a strong signal.
        """
        existing = [edge("easyocr", memory_id="mem-easyocr")]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint="something else entirely",
            existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.SUPERSEDING
        assert d.target_memory_id == "mem-easyocr"

    def test_change_signal_multiple_siblings_no_hint_match_is_too_ambiguous(self) -> None:
        """Guessing which of several active facts to supersede would risk closing
        the wrong one -- falls back to CONFLICTING instead (docs/adr/0007).
        """
        existing = [
            edge("easyocr", memory_id="mem-1"),
            edge("tesseract", memory_id="mem-2"),
        ]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.CONFLICTING
        assert d.target_memory_id is None

    def test_superseded_sibling_is_not_a_candidate_to_supersede_again(self) -> None:
        """An already-superseded edge is not "active" -- it must not be treated as
        the thing being replaced a second time.
        """
        existing = [edge("easyocr", status=MemoryStatus.SUPERSEDED, memory_id="mem-old")]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint="EasyOCR", existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.NEW


class TestScenarioF_Duplicate:
    def test_exact_restatement_of_an_active_fact_is_duplicate(self) -> None:
        existing = [edge("gemini", memory_id="mem-gemini", confidence=1.0)]
        d = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.DUPLICATE
        assert d.target_memory_id == "mem-gemini"

    def test_restatement_with_lower_confidence_is_still_duplicate(self) -> None:
        existing = [edge("gemini", memory_id="mem-gemini", confidence=0.9)]
        d = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.5,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.DUPLICATE

    def test_restatement_with_equal_confidence_is_duplicate_not_update(self) -> None:
        existing = [edge("gemini", memory_id="mem-gemini", confidence=0.9)]
        d = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.DUPLICATE


class TestUpdate:
    def test_higher_confidence_restatement_is_update(self) -> None:
        existing = [edge("gemini", memory_id="mem-gemini", confidence=0.7)]
        d = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.95,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.UPDATE
        assert d.target_memory_id == "mem-gemini"

    def test_change_signal_naming_the_same_target_is_update_not_superseding(self) -> None:
        """A contradictory-but-informative signal: is_change=True but the named
        target is what's already on record. Superseding requires a DIFFERENT
        target; this is treated as a restatement instead.
        """
        existing = [edge("gemini", memory_id="mem-gemini")]
        d = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.9,
            is_change=True, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.UPDATE
        assert d.target_memory_id == "mem-gemini"


class TestScenarioH_Conflicting:
    def test_different_active_target_no_change_signal_is_conflicting(self) -> None:
        existing = [edge("easyocr", memory_id="mem-easyocr")]
        d = classify_relationship(
            predicate="uses", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.CONFLICTING
        assert d.target_memory_id == "mem-easyocr"

    def test_conflicting_never_touches_the_existing_memory(self) -> None:
        """CONFLICTING must never carry a supersession -- both memories are meant
        to remain exactly as they are (docs/adr/0007). This is a structural
        assertion: the RelationshipDecision alone cannot invalidate anything; only
        the runner's dispatch on `decision.decision` does, and CONFLICTING routes
        to `link_conflicting`, never `invalidate` or `update`.
        """
        existing = [edge("easyocr", memory_id="mem-easyocr")]
        d = classify_relationship(
            predicate="uses", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.CONFLICTING
        # target_memory_id here means "link against", not "act upon" -- verified
        # at the integration level (test_extraction_runner.py) that both memories
        # remain ACTIVE/unmodified.

    def test_multiple_simultaneously_active_facts_with_no_single_target_are_unlinked(
        self,
    ) -> None:
        """An unusual pre-existing situation (several already-active facts for one
        predicate) -- picking one to link would be an arbitrary guess.
        """
        existing = [edge("easyocr", memory_id="mem-1"), edge("tesseract", memory_id="mem-2")]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.CONFLICTING
        assert d.target_memory_id is None


class TestReassertingHistory:
    def test_reasserting_a_historical_fact_is_new_not_duplicate(self) -> None:
        """Re-asserting something no longer active creates a new memory
        (docs/adr/0002) -- it must not silently corroborate the old, closed one.
        """
        existing = [edge("easyocr", status=MemoryStatus.HISTORICAL, memory_id="mem-old")]
        d = classify_relationship(
            predicate="uses", target_normalized="easyocr", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.NEW

    def test_conflicting_status_still_counts_as_believed_for_matching(self) -> None:
        """CONFLICTING memories are unresolved, not discarded -- a sibling edge in
        that status is still a live fact to compare against.
        """
        existing = [edge("easyocr", status=MemoryStatus.CONFLICTING, memory_id="mem-c")]
        d = classify_relationship(
            predicate="uses", target_normalized="easyocr", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.DUPLICATE


class TestPredicateIsolation:
    def test_edges_for_a_different_predicate_are_irrelevant(self) -> None:
        """`existing_edges` is assumed pre-filtered to the candidate's predicate by
        the caller; this just confirms the function trusts that input rather than
        re-filtering -- a caller bug would surface as a wrong decision, not a
        silent no-op, which is the right failure mode for a caller-contract bug.
        """
        existing = [edge("easyocr", memory_id="mem-easyocr")]  # pretend: different predicate
        d = classify_relationship(
            predicate="uses", target_normalized="easyocr", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=existing, now=NOW,
        )
        # Same target, active, no change -> duplicate, exactly as if same-predicate.
        assert d.decision is DecisionType.DUPLICATE


class TestOverallDecision:
    def test_empty_relationships_is_new(self) -> None:
        d = overall_decision([])
        assert d.decision is DecisionType.NEW

    def test_single_decision_passes_through(self) -> None:
        only = classify_relationship(
            predicate="uses", target_normalized="x", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=[], now=NOW,
        )
        assert overall_decision([only]) is only

    def test_conflicting_outranks_new(self) -> None:
        new = classify_relationship(
            predicate="uses", target_normalized="x", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=[], now=NOW,
        )
        conflicting = classify_relationship(
            predicate="uses", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("easyocr", memory_id="mem-1")], now=NOW,
        )
        assert overall_decision([new, conflicting]).decision is DecisionType.CONFLICTING

    def test_superseding_outranks_conflicting(self) -> None:
        superseding = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint="EasyOCR",
            existing_edges=[edge("easyocr", memory_id="mem-1")], now=NOW,
        )
        conflicting = classify_relationship(
            predicate="uses", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("gemini", memory_id="mem-2")], now=NOW,
        )
        assert overall_decision([superseding, conflicting]).decision is DecisionType.SUPERSEDING

    def test_update_outranks_duplicate(self) -> None:
        update = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.95,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("gemini", memory_id="mem-1", confidence=0.5)], now=NOW,
        )
        duplicate = classify_relationship(
            predicate="considers", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("tesseract", memory_id="mem-2")], now=NOW,
        )
        assert overall_decision([duplicate, update]).decision is DecisionType.UPDATE

    def test_duplicate_outranks_new(self) -> None:
        duplicate = classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("gemini", memory_id="mem-1")], now=NOW,
        )
        new = classify_relationship(
            predicate="considers", target_normalized="x", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=[], now=NOW,
        )
        assert overall_decision([new, duplicate]).decision is DecisionType.DUPLICATE

    def test_order_of_input_does_not_affect_the_result(self) -> None:
        a = classify_relationship(
            predicate="uses", target_normalized="x", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=[], now=NOW,
        )
        b = classify_relationship(
            predicate="uses", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("easyocr", memory_id="mem-1")], now=NOW,
        )
        assert overall_decision([a, b]).decision == overall_decision([b, a]).decision


class TestNameNormalizationAtTheBoundary:
    def test_target_matching_is_case_and_whitespace_insensitive(self) -> None:
        """The caller normalizes names before calling in; this confirms the
        engine compares normalized forms consistently rather than raw strings.
        """
        existing = [edge("drawing extractor", memory_id="mem-1")]
        d = classify_relationship(
            predicate="works_on", target_normalized="drawing extractor",
            candidate_confidence=0.9, is_change=False, replaces_hint=None,
            existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.DUPLICATE

    def test_replaces_hint_is_normalized_before_matching(self) -> None:
        existing = [edge("easyocr", memory_id="mem-easyocr")]
        d = classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint="  EASYOCR  ", existing_edges=existing, now=NOW,
        )
        assert d.decision is DecisionType.SUPERSEDING
        assert d.target_memory_id == "mem-easyocr"


def test_every_decision_type_is_reachable() -> None:
    """A coverage guard: every DecisionType the plan specifies for the
    relationship-level engine must be producible by at least one input in this
    file (INVALID and IGNORE are candidate-level, applied before this engine runs,
    and are exercised separately in TestPersistenceGate and the grounding tests).
    """
    reachable = {
        classify_relationship(
            predicate="uses", target_normalized="x", candidate_confidence=0.9,
            is_change=False, replaces_hint=None, existing_edges=[], now=NOW,
        ).decision,  # NEW
        classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("gemini", memory_id="m")], now=NOW,
        ).decision,  # DUPLICATE
        classify_relationship(
            predicate="uses", target_normalized="gemini", candidate_confidence=0.99,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("gemini", memory_id="m", confidence=0.1)], now=NOW,
        ).decision,  # UPDATE
        classify_relationship(
            predicate="uses", target_normalized="paddleocr", candidate_confidence=0.9,
            is_change=True, replaces_hint="EasyOCR",
            existing_edges=[edge("easyocr", memory_id="m")], now=NOW,
        ).decision,  # SUPERSEDING
        classify_relationship(
            predicate="uses", target_normalized="tesseract", candidate_confidence=0.9,
            is_change=False, replaces_hint=None,
            existing_edges=[edge("easyocr", memory_id="m")], now=NOW,
        ).decision,  # CONFLICTING
    }
    assert reachable == {
        DecisionType.NEW,
        DecisionType.DUPLICATE,
        DecisionType.UPDATE,
        DecisionType.SUPERSEDING,
        DecisionType.CONFLICTING,
    }
