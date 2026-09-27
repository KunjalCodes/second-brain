"""Text-layer PDF parsing (Phase 6, PHASE6_PLAN.md section 1/5/7/25).

`pypdf` is the only file in this codebase allowed to import it -- mirrors
`app/llm/providers/gemini.py`'s "only this file imports the SDK" discipline
for every provider adapter. Pure-Python, no system dependency, no OCR: a
scanned/image-only PDF is detected and rejected explicitly (never silently
producing near-empty chunks), never routed to an OCR step.
"""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.documents.parsers.base import DocumentParseError, ParsedDocument, ParsedPage

#: ~5x what 500 chunks x 2000 chars can use; beyond this the text is not usable anyway.
MAX_EXTRACTED_CHARS = 5_000_000


def parse_pdf(data: bytes, *, max_pages: int, min_text_chars_per_page: int) -> ParsedDocument:
    """Extract text page by page.

    `max_pages` is checked immediately after opening the file (page count
    is known without extracting any text), before the potentially-expensive
    per-page extraction runs -- PHASE6_PLAN.md section 8's bound is enforced
    as early as the format allows, not only in the upload validation step
    that ran before this function was ever called.

    `min_text_chars_per_page` implements section 5's scanned-PDF detection:
    if the *mean* extracted characters per page falls below this floor, the
    PDF is treated as having no usable text layer and rejected outright
    rather than producing near-empty chunks.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, OSError) as exc:
        raise DocumentParseError(
            "malformed_pdf", "Could not read the PDF (the file appears to be damaged)"
        ) from exc

    if page_count == 0:
        raise DocumentParseError("malformed_pdf", "PDF has no pages")
    if page_count > max_pages:
        raise DocumentParseError(
            "too_many_pages", f"PDF has {page_count} pages, exceeding the limit of {max_pages}"
        )

    pages: list[ParsedPage] = []
    extracted = 0
    try:
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            extracted += len(text)
            if extracted > MAX_EXTRACTED_CHARS:
                # Phase 11 (S13): bound memory during extraction. A PDF whose
                # text layer is this large (or a decompression bomb) is
                # refused before the whole text is ever held at once.
                raise DocumentParseError(
                    "text_too_large",
                    f"The PDF's text exceeds {MAX_EXTRACTED_CHARS} characters",
                )
            pages.append(ParsedPage(page_number=index, text=text))
    except (PdfReadError, ValueError) as exc:
        raise DocumentParseError(
            "malformed_pdf", "Could not extract text from the PDF (the file appears to be damaged)"
        ) from exc

    total_chars = sum(len(page.text) for page in pages)
    mean_chars_per_page = total_chars / page_count
    if mean_chars_per_page < min_text_chars_per_page:
        raise DocumentParseError(
            "no_extractable_text",
            f"PDF has no usable text layer (mean {mean_chars_per_page:.1f} "
            f"characters/page, below the {min_text_chars_per_page} floor) -- "
            f"likely a scanned or image-only PDF. OCR is not supported.",
        )

    return ParsedDocument(pages=pages, page_count=page_count)
