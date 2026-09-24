"""Document parser abstraction (Phase 6, PHASE6_PLAN.md section 7).

Pure functions, no I/O beyond the bytes handed in -- exhaustively unit
testable, mirroring `app/extraction/decisions.py`'s "pure, no I/O" shape.
Every parser in `app/documents/parsers/` returns the same `ParsedDocument`
shape regardless of source format, so nothing downstream (chunking,
extraction) needs to know which format produced it.

**Document content is untrusted data**, exactly like conversation message
content (ADR 0007) -- a parser's only job is safe, bounded text extraction;
it never interprets parsed text as anything but data.
"""

from __future__ import annotations

from dataclasses import dataclass


class DocumentParseError(Exception):
    """Parsing failed in a way that should fail the document's ingestion
    explicitly (PHASE6_PLAN.md section 5/6) -- never silently produce
    empty/garbage chunks. `error_code` is a stable, machine-readable reason
    (`unsupported_mime`, `malformed_pdf`, `no_extractable_text`,
    `not_valid_text`, `too_many_pages`), recorded on the `Document` row.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ParsedPage:
    """One page's extracted text. `page_number` is 1-indexed; `None` for
    formats with no page concept (TXT/Markdown) -- never fabricated.
    """

    page_number: int | None
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    """The uniform result every parser produces, regardless of format."""

    pages: list[ParsedPage]
    #: `None` for non-paginated formats (TXT/Markdown); the real page count
    #: for PDF, even if some pages had no extractable text.
    page_count: int | None

    @property
    def full_text(self) -> str:
        """Every page's text, concatenated in order -- the raw material
        chunking (section 6) splits.
        """
        return "\n\n".join(page.text for page in self.pages)
