"""Deterministic chunking (Phase 6, PHASE6_PLAN.md section 6/8) -- pure,
offline, no database.
"""

from __future__ import annotations

from app.documents.chunking import (
    chunk_document,
    strip_repeated_headers_footers,
)
from app.documents.parsers.base import DocumentParseError, ParsedDocument, ParsedPage


def _parsed(pages: list[str]) -> ParsedDocument:
    return ParsedDocument(
        pages=[ParsedPage(page_number=i + 1, text=text) for i, text in enumerate(pages)],
        page_count=len(pages),
    )


class TestHeaderFooterStripping:
    def test_strips_lines_repeated_across_a_majority_of_pages(self) -> None:
        pages = [
            ParsedPage(page_number=1, text="CONFIDENTIAL\nPage one content.\nPage 1"),
            ParsedPage(page_number=2, text="CONFIDENTIAL\nPage two content.\nPage 2"),
            ParsedPage(page_number=3, text="CONFIDENTIAL\nPage three content.\nPage 3"),
        ]
        cleaned = strip_repeated_headers_footers(pages, threshold=0.5)
        for page in cleaned:
            assert "CONFIDENTIAL" not in page.text
        assert "Page one content." in cleaned[0].text
        assert "Page two content." in cleaned[1].text

    def test_leaves_non_repeated_content_untouched(self) -> None:
        pages = [
            ParsedPage(page_number=1, text="Unique content one."),
            ParsedPage(page_number=2, text="Unique content two."),
        ]
        cleaned = strip_repeated_headers_footers(pages, threshold=0.5)
        assert cleaned[0].text == "Unique content one."
        assert cleaned[1].text == "Unique content two."

    def test_single_page_is_a_no_op(self) -> None:
        pages = [ParsedPage(page_number=1, text="Solo page.")]
        assert strip_repeated_headers_footers(pages, threshold=0.5) == pages


class TestChunkDocument:
    def test_chunks_are_deterministic_for_identical_input_and_config(self) -> None:
        parsed = _parsed(["Drawing Extractor uses PaddleOCR. " * 200])
        first = chunk_document(
            parsed, chunk_size_chars=500, overlap_chars=50, header_footer_threshold=0.5
        )
        second = chunk_document(
            parsed, chunk_size_chars=500, overlap_chars=50, header_footer_threshold=0.5
        )
        assert [c.content_hash for c in first] == [c.content_hash for c in second]
        assert [c.char_start for c in first] == [c.char_start for c in second]

    def test_chunk_ordinals_are_stable_and_sequential(self) -> None:
        parsed = _parsed(["word " * 500])
        chunks = chunk_document(
            parsed, chunk_size_chars=300, overlap_chars=30, header_footer_threshold=0.5
        )
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))
        assert len(chunks) > 1

    def test_overlap_means_adjacent_chunks_share_text(self) -> None:
        parsed = _parsed(["A" * 1000])
        chunks = chunk_document(
            parsed, chunk_size_chars=400, overlap_chars=100, header_footer_threshold=0.5
        )
        assert len(chunks) >= 2
        assert chunks[1].char_start < chunks[0].char_end

    def test_empty_document_produces_no_chunks(self) -> None:
        parsed = _parsed(["   ", ""])
        chunks = chunk_document(
            parsed, chunk_size_chars=500, overlap_chars=50, header_footer_threshold=0.5
        )
        assert chunks == []

    def test_page_metadata_is_preserved_per_chunk(self) -> None:
        parsed = _parsed(["Page one text about EasyOCR.", "Page two text about PaddleOCR."])
        chunks = chunk_document(
            parsed, chunk_size_chars=20, overlap_chars=2, header_footer_threshold=0.5
        )
        pages_seen = {c.page_start for c in chunks} | {c.page_end for c in chunks}
        assert pages_seen == {1, 2}

    def test_heading_context_is_attached_for_markdown_style_headings(self) -> None:
        text = (
            "# Intro\n\nIntro text here that is reasonably long.\n\n"
            "## Background\n\nBackground text that is also long enough to be its own chunk."
        )
        parsed = _parsed([text])
        chunks = chunk_document(
            parsed, chunk_size_chars=60, overlap_chars=5, header_footer_threshold=0.5
        )
        headings = [c.heading_context for c in chunks]
        assert "Intro" in headings
        assert any(h is not None and "Background" in h for h in headings)

    def test_max_chunks_limit_is_enforced(self) -> None:
        parsed = _parsed(["word " * 5000])
        try:
            chunk_document(
                parsed, chunk_size_chars=50, overlap_chars=5,
                header_footer_threshold=0.5, max_chunks=3,
            )
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "too_many_chunks"

    def test_never_loops_forever_even_with_aggressive_overlap(self) -> None:
        """overlap_chars >= chunk_size_chars must not create an infinite
        loop -- forward progress is guaranteed regardless of configuration.
        """
        parsed = _parsed(["word " * 200])
        chunks = chunk_document(
            parsed, chunk_size_chars=50, overlap_chars=1000, header_footer_threshold=0.5
        )
        assert len(chunks) > 0
