"""Deterministic memory-worthiness policy (Phase 14, M1)."""

from __future__ import annotations

import pytest

from app.extraction.worthiness import evaluate_worthiness
from app.models.extraction import CandidateMemory, Span


def _candidate(persistence: str) -> CandidateMemory:
    return CandidateMemory(
        statement="x",
        memory_type="semantic",
        persistence=persistence,
        confidence=0.9,
        source_message_id="m1",
        source_span=Span(start=0, end=1),
        reasoning="r",
    )


@pytest.mark.parametrize(
    ("text", "persistence", "store", "code"),
    [
        # temporary context wins over a "persistent" label
        (
            "Right now I'm debugging the Atlas login page.",
            "persistent",
            False,
            "transient_activity",
        ),
        ("I'm trying to get the build green.", "persistent", False, "transient_activity"),
        (
            "At the moment I am investigating the flaky test.",
            "persistent",
            False,
            "transient_activity",
        ),
        (
            "If we ever move Atlas to AWS, we would need Terraform.",
            "persistent",
            False,
            "hypothetical",
        ),
        ("What if we used Rust for the parser instead?", "persistent", False, "hypothetical"),
        ("So 12 times 8 is 96, which covers the batch.", "persistent", False, "calculation"),
        ("3 + 4 = 7 so we have room.", "persistent", False, "calculation"),
        # explicit instructions override a hedged label
        (
            "Remember that I always deploy Atlas on Tuesdays.",
            "temporary",
            True,
            "explicit_user_instruction",
        ),
        (
            "From now on, always answer me in British English.",
            "uncertain",
            True,
            "explicit_user_instruction",
        ),
        (
            "Don't forget the staging database is read-only.",
            "temporary",
            True,
            "explicit_user_instruction",
        ),
        ("I prefer dark mode in every editor.", "uncertain", True, "explicit_user_instruction"),
        # otherwise the model decides, as before
        (
            "Atlas currently runs on Kubernetes in production.",
            "persistent",
            True,
            "model_persistent",
        ),
        ("Atlas uses PostgreSQL for storage.", "persistent", True, "model_persistent"),
        ("I'm exhausted after debugging all day.", "temporary", False, "model_temporary"),
        ("Not sure about that one.", "uncertain", False, "model_uncertain"),
    ],
)
def test_rules(text: str, persistence: str, store: bool, code: str) -> None:
    w = evaluate_worthiness(_candidate(persistence), target_text=text)
    assert (w.store, w.code) == (store, code), w.reason
    assert w.reason
    assert w.tag() == f"[worthiness:{code}]"


def test_a_current_state_is_not_a_transient_activity() -> None:
    """ "currently" alone describes a state worth keeping; only an activity
    verb makes it transient."""
    w = evaluate_worthiness(
        _candidate("persistent"), target_text="We currently host Atlas on Render."
    )
    assert w.store


def test_always_in_a_mood_statement_is_not_an_instruction() -> None:
    w = evaluate_worthiness(_candidate("temporary"), target_text="I'm always tired on Mondays.")
    assert not w.store
    assert w.code == "model_temporary"


def test_a_recurring_activity_is_not_transient() -> None:
    w = evaluate_worthiness(
        _candidate("persistent"), target_text="I'm testing Cutlist Optimizer every Friday."
    )
    assert w.store
    assert w.code == "model_persistent"
