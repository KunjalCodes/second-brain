"""Entity ambiguity detection (Scenario G): detect, never merge. Pure, no I/O."""

from __future__ import annotations

import pytest

from app.extraction.resolution import canonical_key, is_plausible_duplicate


class TestCanonicalKey:
    def test_strips_punctuation_and_whitespace(self) -> None:
        assert canonical_key("Open AI") == canonical_key("OpenAI") == "openai"

    def test_case_insensitive(self) -> None:
        assert canonical_key("EASYOCR") == canonical_key("easyocr")


class TestPlausibleDuplicate:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Open AI", "OpenAI"),
            ("Data Base", "Database"),
            ("Draw-ing Extractor", "Drawing Extractor"),
        ],
    )
    def test_flags_punctuation_and_whitespace_variants(self, a: str, b: str) -> None:
        assert is_plausible_duplicate(a, b) is True

    def test_unrelated_names_are_not_flagged(self) -> None:
        assert is_plausible_duplicate("EasyOCR", "PaddleOCR") is False

    def test_already_equal_under_normalize_name_is_not_flagged(self) -> None:
        """Phase 1's own exact-match resolution already treats these as one
        entity -- there is nothing ambiguous left to report.
        """
        assert is_plausible_duplicate("EasyOCR", "easyocr") is False
        assert is_plausible_duplicate("  Drawing   Extractor  ", "Drawing Extractor") is False

    def test_is_symmetric(self) -> None:
        assert is_plausible_duplicate("Open AI", "OpenAI") == is_plausible_duplicate(
            "OpenAI", "Open AI"
        )

    def test_identical_strings_are_not_flagged(self) -> None:
        assert is_plausible_duplicate("OpenAI", "OpenAI") is False

    def test_never_merges_only_flags(self) -> None:
        """Structural guarantee: this module has no function that could plausibly
        merge or rename an entity -- only detection functions exist.
        """
        import app.extraction.resolution as module

        public_names = [n for n in dir(module) if not n.startswith("_")]
        forbidden_verbs = ("merge", "rename", "delete", "replace")
        for name in public_names:
            assert not any(verb in name.lower() for verb in forbidden_verbs), name


class TestPhase14UnicodeAndShortKeys:
    """Phase 14 (M3): false merge candidates are worse than missed ones."""

    def test_distinct_non_latin_names_are_not_duplicates(self) -> None:
        # the old ASCII-only key reduced both to "" -> "duplicate"
        assert is_plausible_duplicate("東京", "大阪") is False

    def test_non_latin_spacing_variant_is_still_flagged(self) -> None:
        assert is_plausible_duplicate("東京タワー", "東京 タワー") is True

    def test_diacritic_variant_is_flagged(self) -> None:
        assert is_plausible_duplicate("Zoë", "Zoe") is True

    def test_keys_too_short_to_be_evidence_are_never_flagged(self) -> None:
        assert is_plausible_duplicate("C++", "C#") is False
        assert is_plausible_duplicate("C", "c.") is False
