"""Parser dispatch by content type (Phase 6, PHASE6_PLAN.md section 1/7)."""

from __future__ import annotations

from app.documents.parsers.base import DocumentParseError, ParsedDocument
from app.documents.parsers.pdf import parse_pdf
from app.documents.parsers.text import parse_markdown, parse_txt

#: Content types Phase 6 v1 supports -- never every format (PHASE6_PLAN.md
#: section 1/20). Checked against the content-sniffed MIME
#: (`app/documents/validation.py`), never a client-supplied one.
SUPPORTED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"text/plain", "text/markdown", "application/pdf"}
)


def parse_document(
    data: bytes, *, content_type: str, max_pdf_pages: int, min_pdf_text_chars_per_page: int
) -> ParsedDocument:
    if content_type == "text/plain":
        return parse_txt(data)
    if content_type == "text/markdown":
        return parse_markdown(data)
    if content_type == "application/pdf":
        return parse_pdf(
            data, max_pages=max_pdf_pages, min_text_chars_per_page=min_pdf_text_chars_per_page
        )
    raise DocumentParseError(
        "unsupported_mime", f"No parser is registered for content type {content_type!r}"
    )


__all__ = ["SUPPORTED_CONTENT_TYPES", "DocumentParseError", "ParsedDocument", "parse_document"]
