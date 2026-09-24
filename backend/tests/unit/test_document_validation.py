"""Upload validation (Phase 6, PHASE6_PLAN.md sections 1/6/9) -- pure,
offline, no database.
"""

from __future__ import annotations

from app.documents.parsers.base import DocumentParseError
from app.documents.validation import sanitize_filename, sniff_content_type, validate_upload
from tests.fixtures.pdf_builder import build_pdf


class TestSanitizeFilename:
    def test_keeps_a_normal_filename(self) -> None:
        assert sanitize_filename("report.pdf") == "report.pdf"

    def test_strips_path_components_path_traversal(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("/etc/shadow") == "shadow"
        assert sanitize_filename("..\\..\\windows\\system32\\config") == "config"

    def test_strips_control_characters(self) -> None:
        assert "\x00" not in sanitize_filename("evil\x00name.txt")

    def test_empty_or_all_unsafe_falls_back_to_untitled(self) -> None:
        assert sanitize_filename("") == "untitled"
        assert sanitize_filename("///") == "untitled"

    def test_bounds_length(self) -> None:
        assert len(sanitize_filename("a" * 1000 + ".txt")) <= 255


class TestSniffContentType:
    def test_pdf_magic_bytes_win_regardless_of_extension(self) -> None:
        pdf = build_pdf(["hello"])
        assert sniff_content_type(pdf, filename="notes.txt") == "application/pdf"

    def test_markdown_extension_labels_valid_text_as_markdown(self) -> None:
        assert sniff_content_type(b"# Title", filename="notes.md") == "text/markdown"

    def test_txt_extension_labels_valid_text_as_plain(self) -> None:
        assert sniff_content_type(b"just text", filename="notes.txt") == "text/plain"

    def test_binary_content_is_rejected_regardless_of_extension(self) -> None:
        """The extension claiming `.txt` must not override a real safety
        check -- content sniffing, never the extension, decides safety.
        """
        try:
            sniff_content_type(b"\x00\x01\x02binary", filename="totally-safe.txt")
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "unsupported_mime"


class TestValidateUpload:
    def test_accepts_a_valid_text_file(self) -> None:
        result = validate_upload(b"hello world", filename="notes.txt", max_size_bytes=1000)
        assert result.content_type == "text/plain"
        assert result.sanitized_filename == "notes.txt"

    def test_rejects_empty_file(self) -> None:
        try:
            validate_upload(b"", filename="empty.txt", max_size_bytes=1000)
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "empty_document"

    def test_rejects_oversized_file(self) -> None:
        try:
            validate_upload(b"x" * 2000, filename="big.txt", max_size_bytes=1000)
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "file_too_large"

    def test_rejects_disguised_binary_file(self) -> None:
        """A binary file renamed to .txt must not pass validation --
        content is sniffed, the extension is never trusted for safety.
        """
        try:
            validate_upload(
                b"\x89PNG\r\n\x1a\nbinary image data",
                filename="totally-a-text-file.txt",
                max_size_bytes=1000,
            )
            raise AssertionError("expected DocumentParseError")
        except DocumentParseError as exc:
            assert exc.error_code == "unsupported_mime"

    def test_sanitizes_a_path_traversal_filename_while_accepting_the_content(self) -> None:
        result = validate_upload(b"hello", filename="../../etc/passwd.txt", max_size_bytes=1000)
        assert result.sanitized_filename == "passwd.txt"
