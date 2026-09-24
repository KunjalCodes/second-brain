"""TXT and Markdown parsing (Phase 6, PHASE6_PLAN.md section 1/7).

Trivial by design -- stdlib decode only, no rendering library, since headings
only need *detecting* (for chunk metadata, `app/documents/chunking.py`), not
rendering.
"""

from __future__ import annotations

from app.documents.parsers.base import DocumentParseError, ParsedDocument, ParsedPage

#: A binary file's bytes almost always contain NUL, which is never valid in
#: real text content -- the cheapest, most reliable "this isn't text at all"
#: signal, checked before attempting a full UTF-8 decode.
_NUL_BYTE = b"\x00"


def _decode_or_raise(data: bytes, *, error_code_prefix: str) -> str:
    if _NUL_BYTE in data:
        raise DocumentParseError(
            f"{error_code_prefix}_not_valid_text",
            "The uploaded file contains binary data and is not valid text",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentParseError(
            f"{error_code_prefix}_not_valid_text",
            "The uploaded file is not valid UTF-8 text",
        ) from exc


def parse_txt(data: bytes) -> ParsedDocument:
    text = _decode_or_raise(data, error_code_prefix="txt")
    return ParsedDocument(pages=[ParsedPage(page_number=None, text=text)], page_count=None)


def parse_markdown(data: bytes) -> ParsedDocument:
    """Identical to `parse_txt` -- Markdown headings are detected later, by
    `app/documents/chunking.py`, directly from the text (`^#{1,6} `), not
    here. No Markdown-rendering library is used: nothing downstream ever
    renders this document, only chunks and extracts facts from its text.
    """
    text = _decode_or_raise(data, error_code_prefix="markdown")
    return ParsedDocument(pages=[ParsedPage(page_number=None, text=text)], page_count=None)
