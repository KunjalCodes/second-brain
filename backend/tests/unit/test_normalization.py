"""Deterministic entity-name normalization (Phase 1 entity resolution)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.models.normalization import normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Drawing Extractor", "drawing extractor"),
        ("  Drawing   Extractor  ", "drawing extractor"),
        ("EASYOCR", "easyocr"),
        ("easyocr", "easyocr"),
        ("Drawing\tExtractor", "drawing extractor"),
        ("Drawing\nExtractor", "drawing extractor"),
    ],
)
def test_normalizes_case_and_whitespace(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_case_variants_resolve_together() -> None:
    assert normalize_name("OpenAI") == normalize_name("openai") == "openai"


def test_internal_spaces_are_preserved_deliberately() -> None:
    """"Open AI" does NOT collapse to "openai".

    Stripping internal spaces would also merge "Data Base" with "Database". Wrongly
    merging two entities destroys memory, so Phase 1 requires an explicit alias for
    this case instead of guessing.
    """
    assert normalize_name("Open AI") != normalize_name("OpenAI")


@pytest.mark.parametrize("raw", ["", "   ", "\t", "\n"])
def test_rejects_blank_names(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_name(raw)
