"""The repair prompt names what failed validation, without the rejected values."""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.core.errors import LLMResponseInvalidError
from app.extraction.runner import _schema_problems
from app.models.extraction import ExtractionResult

SECRET_LIKE = "Priya's private note " * 200  # > 2000 chars: fails the statement bound


def _invalid() -> LLMResponseInvalidError:
    candidate = {
        "statement": SECRET_LIKE,
        "memory_type": "nonsense",
        "persistence": "persistent",
        "confidence": 3.5,
        "entities": [],
        "relationships": [],
        "temporal": {},
        "source_message_id": "m1",
        "source_span": {"start": 0, "end": 1},
        "reasoning": "r",
    }
    raw = json.dumps({"candidates": [candidate]})
    try:
        ExtractionResult.model_validate_json(raw)
    except ValidationError as exc:
        return LLMResponseInvalidError("bad", details={"errors": exc.errors()})
    raise AssertionError("expected a validation error")


def test_names_the_failing_fields() -> None:
    problems = _schema_problems(_invalid())
    assert "candidates.0.statement" in problems
    assert "candidates.0.confidence" in problems
    assert "candidates.0.memory_type" in problems


def test_never_includes_the_rejected_values() -> None:
    problems = _schema_problems(_invalid())
    assert "Priya" not in problems
    assert "nonsense" not in problems
    assert "3.5" not in problems


def test_is_empty_without_structured_errors() -> None:
    assert _schema_problems(LLMResponseInvalidError("bad")) == ""
    assert _schema_problems(LLMResponseInvalidError("bad", details={"errors": "x"})) == ""


def test_is_bounded() -> None:
    errors = [{"loc": ("candidates", i), "msg": "Field required"} for i in range(20)]
    problems = _schema_problems(LLMResponseInvalidError("bad", details={"errors": errors}))
    assert problems.count("Field required") == 8
    assert problems.endswith("and 12 more")
