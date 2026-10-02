# ruff: noqa: E501 - compact event tables
"""Phase 16 agent events: validation at the boundary and the pure event -> fact mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.enums import EntityType
from app.models.project_history import PRIMARY_TEXT_KEY, AgentEventType, event_text
from app.projects.events import (
    MAX_EVENTS_PER_REQUEST,
    MAX_PAYLOAD_BYTES,
    PAYLOAD_MODELS,
    EventBatch,
    EventIn,
    event_fingerprint,
)
from app.projects.projection import MAX_STATEMENT, build_fact

T = "2026-10-01T10:00:00Z"
EID = "00000000-0000-4000-8000-000000000001"


def ev(event_type: str, payload: dict, **extra) -> dict:  # noqa: ANN003
    return {"session_id": "s1", "event_type": event_type, "timestamp": T, "payload": payload, **extra}


def parse(event_type: str, payload: dict, **extra) -> EventIn:  # noqa: ANN003
    return EventIn.model_validate(ev(event_type, payload, **extra))


# -- the boundary ------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["user_id", "project_id", "space_id", "owner_user_id", "id", "memory_id"])
def test_authority_fields_are_rejected_on_the_envelope(field: str) -> None:
    with pytest.raises(ValidationError):
        EventIn.model_validate(ev("user_request", {"text": "x"}, **{field: "someone-else"}))


@pytest.mark.parametrize("field", ["user_id", "project_id", "space_id", "session_id", "memory_id"])
def test_authority_fields_are_rejected_inside_every_payload(field: str) -> None:
    minimal = {
        "session_started": {}, "session_ended": {"outcome": "completed"}, "user_request": {"text": "x"},
        "decision": {"title": "x"}, "file_inspected": {"path": "a.py"}, "file_modified": {"path": "a.py"},
        "bug_discovered": {"title": "x"}, "error": {"message": "x"},
        "fix": {"title": "x", "outcome": "succeeded"}, "test_run": {"command": "pytest", "result": "passed"},
        "commit": {"sha": "a" * 40, "message": "x"}, "task_completed": {"summary": "x"},
        "task_blocked": {"reason": "x"}, "memory_proposal": {"statement": "x"},
    }  # fmt: skip
    assert set(minimal) == {t.value for t in AgentEventType}  # every type covered
    for event_type, payload in minimal.items():
        EventIn.model_validate(ev(event_type, payload))  # valid as written
        with pytest.raises(ValidationError):
            EventIn.model_validate(ev(event_type, {**payload, field: "x"}))


def test_every_event_type_has_a_payload_model_and_a_primary_text_key() -> None:
    assert set(PAYLOAD_MODELS) == set(AgentEventType) == set(PRIMARY_TEXT_KEY)


def test_event_type_is_case_insensitive_and_canonicalised() -> None:
    assert parse("DECISION", {"title": "x"}).event_type is AgentEventType.DECISION
    assert parse(" Decision ", {"title": "x"}).event_type is AgentEventType.DECISION


@pytest.mark.parametrize("bad", ["", "nope", "tool_call", "shell", "decision;drop"])
def test_unknown_event_types_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        parse(bad, {})


def test_timestamp_needs_a_timezone_and_is_normalised_to_utc() -> None:
    with pytest.raises(ValidationError):
        EventIn.model_validate({**ev("user_request", {"text": "x"}), "timestamp": "2026-10-01T10:00:00"})
    e = EventIn.model_validate({**ev("user_request", {"text": "x"}), "timestamp": "2026-10-01T12:00:00+02:00"})
    assert e.timestamp.isoformat() == "2026-10-01T10:00:00+00:00"


def test_payload_is_normalised_and_cleaned() -> None:
    e = parse(
        "decision",
        {"title": "  Use ‮PG‬   queue ", "files": ["./src//a.py"], "rationale": "a\r\n\r\n\r\nb"},
    )
    assert e.payload["title"] == "Use PG queue"
    assert e.payload["files"] == ["src/a.py"]
    assert e.payload["rationale"] == "a\nb"
    assert "feature" not in e.payload  # absent stays absent


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("decision", {"title": "x" * 161}),
        ("decision", {"title": "x", "files": [f"f{i}.py" for i in range(11)]}),
        ("decision", {"title": "x", "alternatives": ["a"] * 6}),
        ("decision", {"title": "x", "files": ["../etc/passwd"]}),
        ("decision", {"title": "x", "files": ["/abs/path"]}),
        ("decision", {"title": "x", "files": [".git/config"]}),
        ("file_inspected", {"path": "a\x00b"}),
        ("file_modified", {"path": "a.py", "change": "renamed"}),  # needs old_path
        ("file_modified", {"path": "a.py", "old_path": "b.py"}),  # old_path needs renamed
        ("fix", {"title": "x"}),  # outcome is REQUIRED: never inferred
        ("fix", {"title": "x", "outcome": "maybe"}),
        ("session_ended", {}),
        ("session_ended", {"outcome": "completed", "unfinished": ["x"] * 11}),
        ("test_run", {"command": "pytest", "result": "passed", "passed": -1}),
        ("commit", {"sha": "xyz", "message": "m"}),
        ("commit", {"sha": "a" * 40, "message": "m", "files": ["a.py"] * 16}),
        ("user_request", {"text": ""}),
        ("user_request", {"text": "   \x00  "}),
    ],
)
def test_malformed_payloads_are_rejected(event_type: str, payload: dict) -> None:
    with pytest.raises(ValidationError):
        parse(event_type, payload)


def test_oversized_payloads_are_rejected_before_parsing() -> None:
    with pytest.raises(ValidationError):
        parse("user_request", {"text": "x", "junk": "y" * MAX_PAYLOAD_BYTES})


def test_batches_are_bounded_and_non_empty() -> None:
    one = ev("user_request", {"text": "x"})
    EventBatch.model_validate({"events": [one] * MAX_EVENTS_PER_REQUEST})
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": [one] * (MAX_EVENTS_PER_REQUEST + 1)})
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": []})


def test_one_bad_event_invalidates_the_whole_batch() -> None:
    with pytest.raises(ValidationError):
        EventBatch.model_validate(
            {"events": [ev("user_request", {"text": "ok"}), ev("fix", {"title": "no outcome"})]}
        )


@pytest.mark.parametrize("bad", ["", "-x", "a b", "a/b", "x" * 129, "é"])
def test_client_event_ids_are_constrained(bad: str) -> None:
    with pytest.raises(ValidationError):
        parse("user_request", {"text": "x"}, event_id=bad)


def test_fingerprint_is_stable_and_content_sensitive() -> None:
    a = parse("decision", {"title": "x", "files": ["a.py"]})
    b = EventIn.model_validate(
        {"payload": {"files": ["a.py"], "title": "x"}, "timestamp": T, "event_type": "decision", "session_id": "s1"}
    )
    assert event_fingerprint(a) == event_fingerprint(b)  # key order does not matter
    assert event_fingerprint(a) != event_fingerprint(parse("decision", {"title": "y"}))
    later = EventIn.model_validate({**ev("decision", {"title": "x", "files": ["a.py"]}), "timestamp": "2026-10-01T10:00:01Z"})
    assert event_fingerprint(a) != event_fingerprint(later)
    assert event_fingerprint(a) == event_fingerprint(parse("decision", {"title": "x", "files": ["a.py"]}, source="other"))


def test_event_text_reads_the_primary_field() -> None:
    assert event_text(AgentEventType.DECISION, {"title": "T"}) == "T"
    assert event_text(AgentEventType.ERROR, {}) is None


# -- the pure mapping --------------------------------------------------------------------------


def fact(event_type: str, payload: dict):  # noqa: ANN201
    e = parse(event_type, payload)
    return build_fact(event_type=e.event_type, payload=e.payload, session_key="s1", event_id=EID)


def rels(f) -> set[tuple[str, str, str]]:  # noqa: ANN001
    return {(r.source, r.predicate, r.target) for r in f.candidate.relationships}


def kinds(f) -> dict[str, EntityType]:  # noqa: ANN001
    return {e.name: e.entity_type for e in f.candidate.entities}


def test_decision_records_what_was_stated_and_nothing_else() -> None:
    f = fact("decision", {"title": "Use PG queue", "rationale": "No Redis in prod", "files": ["q.py"], "feature": "Jobs"})
    assert rels(f) == {
        ("session:s1", "made", "decision:Use PG queue"),
        ("decision:Use PG queue", "affected", "file:q.py"),
        ("decision:Use PG queue", "affected", "feature:Jobs"),
    }
    assert kinds(f)["decision:Use PG queue"] is EntityType.DECISION
    assert "Rationale (stated by the agent): No Redis in prod" in f.candidate.statement
    assert f.embed and f.supersedes_title is None


def test_a_decision_without_a_rationale_never_gains_one() -> None:
    f = fact("decision", {"title": "Use PG queue"})
    assert "Rationale" not in f.candidate.statement and "because" not in f.candidate.statement.lower()


def test_supersession_is_explicit_and_keeps_both_decisions() -> None:
    f = fact("decision", {"title": "B", "supersedes_decision": "A"})
    assert ("decision:A", "superseded_by", "decision:B") in rels(f)
    assert f.supersedes_title == "A" and f.candidate.temporal.is_change
    assert {"decision:A", "decision:B"} <= set(kinds(f))


@pytest.mark.parametrize(
    ("outcome", "predicate"),
    [("succeeded", "fixed_by"), ("partial", "partially_fixed_by"), ("failed", "attempted_fix")],
)
def test_a_fix_links_with_its_STATED_outcome(outcome: str, predicate: str) -> None:  # noqa: N802
    f = fact("fix", {"title": "Lengthen TTL", "outcome": outcome, "fixes_error": "Timeout", "fixes_bug": "Session timeout"})
    assert ("error:Timeout", predicate, "fix:Lengthen TTL") in rels(f)
    assert ("bug:Session timeout", predicate, "fix:Lengthen TTL") in rels(f)
    assert not any(p == "fixed_by" for _, p, _ in rels(f)) or outcome == "succeeded"


def test_no_link_is_invented_when_the_event_does_not_state_one() -> None:
    f = fact("error", {"message": "KeyError: sid"})
    assert rels(f) == {("session:s1", "encountered", "error:KeyError: sid")}
    f = fact("fix", {"title": "Patch", "outcome": "succeeded"})
    assert rels(f) == {("session:s1", "made", "fix:Patch")}  # fixes nothing it did not name
    f = fact("file_modified", {"path": "a.py"})
    assert rels(f) == {("session:s1", "modified", "file:a.py")}
    assert not any(p in {"fixed_by", "led_to", "attempted_fix", "caused_by"} for _, p, _ in rels(f))


def test_a_file_change_alone_is_never_a_failed_attempt() -> None:
    for t, p in [("file_modified", {"path": "a.py"}), ("file_inspected", {"path": "a.py"})]:
        statement = fact(t, p).candidate.statement.lower()
        assert "fail" not in statement and "attempt" not in statement


def test_error_titles_default_to_the_first_line_and_equal_errors_share_an_entity() -> None:
    a = fact("error", {"message": "KeyError: sid\nTraceback ...\n  line 3"})
    b = fact("error", {"message": "KeyError: sid\nTraceback (other)"})
    assert set(kinds(a)) & set(kinds(b)) >= {"error:KeyError: sid"}


def test_error_can_state_the_bug_it_is_a_symptom_of() -> None:
    f = fact("error", {"message": "boom", "bug": "Session timeout", "files": ["s.py"]})
    assert ("bug:Session timeout", "manifested_as", "error:boom") in rels(f)
    assert ("error:boom", "affected", "file:s.py") in rels(f)


def test_test_runs_validate_only_what_they_name_and_only_when_they_pass() -> None:
    passed = fact("test_run", {"command": "pytest", "result": "passed", "validates": ["Patch"]})
    failed = fact("test_run", {"command": "pytest", "result": "failed", "validates": ["Patch"]})
    assert any(p == "validated" for _, p, _ in rels(passed))
    assert any(p == "failed_on" for _, p, _ in rels(failed)) and not any(p == "validated" for _, p, _ in rels(failed))
    assert not any(p in {"validated", "failed_on"} for _, p, _ in rels(fact("test_run", {"command": "pytest", "result": "passed"})))


def test_structural_events_skip_embeddings_and_semantic_ones_keep_them() -> None:
    assert not fact("file_inspected", {"path": "a.py"}).embed
    assert not fact("file_modified", {"path": "a.py"}).embed
    assert not fact("test_run", {"command": "x", "result": "passed"}).embed
    for t, p in [
        ("user_request", {"text": "x"}), ("decision", {"title": "x"}), ("bug_discovered", {"title": "x"}),
        ("error", {"message": "x"}), ("fix", {"title": "x", "outcome": "failed"}),
        ("task_completed", {"summary": "x"}), ("task_blocked", {"reason": "x"}),
        ("commit", {"sha": "a" * 40, "message": "x"}), ("session_ended", {"outcome": "blocked"}),
    ]:  # fmt: skip
        assert fact(t, p).embed, t


def test_a_bare_session_start_is_skipped_with_a_reason() -> None:
    f = fact("session_started", {})
    assert f.candidate is None and f.skip_reason
    assert fact("session_started", {"goal": "Fix auth"}).candidate is not None
    c = fact("session_started", {"continues_session": "s0"})
    assert ("session:s1", "continues", "session:s0") in rels(c)


def test_session_end_records_explicit_unfinished_work_only() -> None:
    f = fact("session_ended", {"outcome": "blocked", "summary": "Stopped", "unfinished": ["Write tests", "Update docs"]})
    assert "Unfinished: Write tests; Update docs." in f.candidate.statement
    assert "Unfinished" not in fact("session_ended", {"outcome": "completed"}).candidate.statement


def test_every_fact_is_attributed_to_the_agent_session_with_agent_confidence() -> None:
    f = fact("bug_discovered", {"title": "x"})
    assert f.candidate.confidence == 0.9 and "session:s1" in kinds(f)
    assert kinds(f)["session:s1"] is EntityType.AGENT_SESSION


def test_worst_case_events_still_build_a_valid_bounded_candidate() -> None:
    worst = {
        "decision": {"title": "t" * 160, "rationale": "r" * 1000, "alternatives": ["a" * 200] * 5,
                     "files": [f"d/{i}/{'f' * 150}.py" for i in range(10)], "feature": "f" * 160,
                     "supersedes_decision": "s" * 160},
        "commit": {"sha": "a" * 40, "message": "m" * 500, "files": [f"d/{i}/{'f' * 150}.py" for i in range(15)]},
        "fix": {"title": "t" * 160, "description": "d" * 1000, "outcome": "failed", "fixes_error": "e" * 160,
                "fixes_bug": "b" * 160, "files": [f"d/{i}/{'f' * 150}.py" for i in range(8)], "approach": "a" * 200},
        "error": {"message": "m" * 1000, "command": "c" * 300, "bug": "b" * 160, "approach": "a" * 200,
                  "files": [f"d/{i}/{'f' * 150}.py" for i in range(8)]},
        "test_run": {"command": "c" * 300, "result": "failed", "summary": "s" * 500, "validates": ["v" * 160] * 5,
                     "files": [f"d/{i}/{'f' * 150}.py" for i in range(8)]},
    }  # fmt: skip
    for t, p in worst.items():
        f = fact(t, p)
        assert len(f.candidate.statement) <= MAX_STATEMENT
        assert len(f.candidate.entities) <= 20 and len(f.candidate.relationships) <= 20
        assert all(len(e.name) <= 200 for e in f.candidate.entities)


# -- memory proposals (M25) -----------------------------------------------------------------------


def proposal(**kw) -> dict:  # noqa: ANN003
    return {"statement": "The queue uses PostgreSQL", "entities": [{"name": "Queue", "entity_type": "technology"}, {"name": "PostgreSQL", "entity_type": "technology"}], "relationships": [{"source": "Queue", "predicate": "uses", "target": "PostgreSQL"}], **kw}


@pytest.mark.parametrize("kind", ["file", "directory", "commit", "agent_session", "branch", "repository", "code_symbol", "dependency", "test_run", "project", "change"])
def test_structural_entity_types_cannot_be_proposed(kind: str) -> None:
    with pytest.raises(ValidationError):
        parse("memory_proposal", {"statement": "x", "entities": [{"name": "src/a.py", "entity_type": kind}]})


def test_proposal_bounds_and_integrity() -> None:
    parse("memory_proposal", proposal())
    for bad in [
        proposal(confidence=0.95),  # an agent cannot claim near-certainty
        proposal(relationships=[{"source": "Queue", "predicate": "uses", "target": "Ghost"}]),
        proposal(relationships=[{"source": "Queue", "predicate": "Uses It!", "target": "PostgreSQL"}]),
        proposal(entities=[{"name": "A", "entity_type": "person"}, {"name": "a", "entity_type": "person"}], relationships=[]),
        proposal(memory_type="working"),
        proposal(user_id="x"),
        proposal(entities=[{"name": "n", "entity_type": "person"}] * 11, relationships=[]),
    ]:
        with pytest.raises(ValidationError):
            parse("memory_proposal", bad)


def test_a_proposal_maps_to_a_governed_candidate_with_capped_confidence() -> None:
    f = fact("memory_proposal", proposal(confidence=0.8, memory_type="procedural"))
    assert f.candidate.confidence == 0.8 and f.embed and f.candidate.memory_type.value == "procedural"
    assert ("Queue", "uses", "PostgreSQL") in rels(f)
    assert all(r.confidence == 0.8 for r in f.candidate.relationships)
    assert "session:s1" not in kinds(f)  # attribution is the event source, not a graph claim


def test_proposals_about_bugs_reach_the_same_entity_a_typed_event_created() -> None:
    f = fact("memory_proposal", {"statement": "Bug is caused by TTL", "entities": [{"name": "Session timeout", "entity_type": "bug"}, {"name": "TTL", "entity_type": "concept"}], "relationships": [{"source": "Session timeout", "predicate": "related_to", "target": "TTL"}]})
    assert "bug:Session timeout" in kinds(f) and ("bug:Session timeout", "related_to", "TTL") in rels(f)
    typed = fact("bug_discovered", {"title": "Session timeout"})
    assert "bug:Session timeout" in kinds(typed)
