"""Parser abstraction (Phase 6, PHASE6_PLAN.md sections 5/7) -- pure,
offline, no database, no network.
"""

from __future__ import annotations

from app.documents.parsers import parse_document
from app.documents.parsers.base import DocumentParseError
from app.documents.parsers.pdf import parse_pdf
from app.documents.parsers.text import parse_markdown, parse_txt
from tests.fixtures.pdf_builder import build_pdf, build_scanned_pdf


class TestTxtParser:
    def test_decodes_utf8_text(self) -> None:
        result = parse_txt(b"Drawing Extractor uses PaddleOCR.")
        assert result.pages[0].text == "Drawing Extractor uses PaddleOCR."
        assert result.page_count is None
        assert result.pages[0].page_number is None

    def test_rejects_binary_data(self) -> None:
        try:
            parse_txt(b"\x00\x01\x02binary garbage")
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "txt_not_valid_text"

    def test_rejects_invalid_utf8(self) -> None:
        try:
            parse_txt(b"\xff\xfe not valid utf-8")
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "txt_not_valid_text"


class TestMarkdownParser:
    def test_decodes_like_txt(self) -> None:
        result = parse_markdown(b"# Title\n\nSome content.")
        assert result.pages[0].text == "# Title\n\nSome content."


class TestPdfParser:
    def test_extracts_text_and_page_count(self) -> None:
        pdf = build_pdf(["Page one about EasyOCR.", "Page two about PaddleOCR."])
        result = parse_pdf(pdf, max_pages=10, min_text_chars_per_page=1)
        assert result.page_count == 2
        assert result.pages[0].page_number == 1
        assert "EasyOCR" in result.pages[0].text
        assert result.pages[1].page_number == 2
        assert "PaddleOCR" in result.pages[1].text

    def test_scanned_pdf_is_rejected_not_silently_empty(self) -> None:
        scanned = build_scanned_pdf(2)
        try:
            parse_pdf(scanned, max_pages=10, min_text_chars_per_page=5)
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "no_extractable_text"

    def test_too_many_pages_is_rejected_before_extraction(self) -> None:
        pdf = build_pdf(["one", "two", "three"])
        try:
            parse_pdf(pdf, max_pages=2, min_text_chars_per_page=1)
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "too_many_pages"

    def test_malformed_pdf_is_rejected_gracefully(self) -> None:
        try:
            parse_pdf(b"not a pdf at all", max_pages=10, min_text_chars_per_page=1)
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "malformed_pdf"


class TestParseDocumentDispatch:
    def test_dispatches_by_content_type(self) -> None:
        result = parse_document(
            b"hello", content_type="text/plain", max_pdf_pages=10, min_pdf_text_chars_per_page=1
        )
        assert result.pages[0].text == "hello"

    def test_unknown_content_type_is_rejected(self) -> None:
        try:
            parse_document(
                b"hello",
                content_type="application/zip",
                max_pdf_pages=10,
                min_pdf_text_chars_per_page=1,
            )
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "unsupported_mime"
