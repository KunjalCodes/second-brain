"""Deterministic document chunking (Phase 6, PHASE6_PLAN.md section 6).

Pure functions, no I/O, no randomness -- identical input plus identical
configuration always produces identical chunks (section 8's explicit
requirement). Fixed-size, character-based splitting with overlap, never
semantic/embedding-based segmentation (section 6's explicit "avoid premature
semantic chunking").
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from app.documents.parsers.base import DocumentParseError, ParsedDocument, ParsedPage

#: How far back from a hard chunk boundary to look for a paragraph or
#: sentence break to snap to instead -- a small, bounded lookback, not a
#: full-text scan.
_BOUNDARY_LOOKBACK_CHARS = 200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ChunkSpec:
    """One chunk, ready to become a `DocumentChunk` row. `char_start`/
    `char_end` are offsets into the (header/footer-stripped) full parsed
    text -- the document-level analogue of `CandidateMemory.source_span`.
    """

    ordinal: int
    content: str
    content_hash: str
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    heading_context: str | None


def strip_repeated_headers_footers(
    pages: list[ParsedPage], *, threshold: float
) -> list[ParsedPage]:
    """Remove lines that repeat verbatim across at least `threshold` of
    pages -- a running header/footer, not real content (PHASE6_PLAN.md
    section 5). A no-op for 0 or 1 pages (there is nothing to repeat
    across), which is also what makes this correctly inert for TXT/Markdown
    (always a single "page").
    """
    if len(pages) < 2:
        return pages

    line_page_counts: Counter[str] = Counter()
    for page in pages:
        distinct_lines = {line.strip() for line in page.text.splitlines() if line.strip()}
        line_page_counts.update(distinct_lines)

    n_pages = len(pages)
    # `count >= 2` is required alongside the proportional check: with few
    # pages and a threshold at or below 1/n_pages, a line appearing on only
    # ONE page can otherwise satisfy "count / n_pages >= threshold" (e.g. 2
    # pages, threshold 0.5, count 1 -> 0.5 >= 0.5) and be wrongly treated as
    # a repeated header/footer even though it never actually repeated.
    repeated = {
        line
        for line, count in line_page_counts.items()
        if count >= 2 and count / n_pages >= threshold
    }
    if not repeated:
        return pages

    cleaned: list[ParsedPage] = []
    for page in pages:
        kept = [line for line in page.text.splitlines() if line.strip() not in repeated]
        cleaned.append(ParsedPage(page_number=page.page_number, text="\n".join(kept)))
    return cleaned


def _page_boundaries(pages: list[ParsedPage]) -> list[tuple[int, int, int | None]]:
    """`(start_offset, end_offset, page_number)` per page, in the same
    `"\\n\\n".join(...)` concatenation `ParsedDocument.full_text` uses.
    """
    boundaries: list[tuple[int, int, int | None]] = []
    offset = 0
    for page in pages:
        start = offset
        end = start + len(page.text)
        boundaries.append((start, end, page.page_number))
        offset = end + 2  # the "\n\n" joiner
    return boundaries


def _page_for_offset(boundaries: list[tuple[int, int, int | None]], offset: int) -> int | None:
    for start, end, page_number in boundaries:
        if start <= offset <= end:
            return page_number
    return boundaries[-1][2] if boundaries else None


def _heading_index(full_text: str) -> list[tuple[int, int, str]]:
    """`(offset, level, heading text)` for every Markdown-style heading
    line, in document order. Applied to every format uniformly (not just
    documents declared as Markdown) -- a PDF or TXT file coincidentally
    containing `"# "`-prefixed lines gets the same treatment, which is
    harmless (PHASE6_PLAN.md section 6: "practical", not format-gated).
    """
    return [
        (match.start(), len(match.group(1)), match.group(2).strip())
        for match in _HEADING_RE.finditer(full_text)
    ]


def _heading_context_at(headings: list[tuple[int, int, str]], offset: int) -> str | None:
    """The heading path ("Intro > Background") in effect at `offset`, from
    every heading at or before it, most recent at each level winning and
    shallower headings clearing any deeper ones that came before them (so a
    new "## " does not keep a stale "### " from an earlier subsection).
    """
    stack: dict[int, str] = {}
    for heading_offset, level, text in headings:
        if heading_offset > offset:
            break
        stack = {lvl: t for lvl, t in stack.items() if lvl < level}
        stack[level] = text
    if not stack:
        return None
    return " > ".join(stack[level] for level in sorted(stack))


def _snap_boundary(text: str, window_floor: int, hard_end: int) -> int:
    """Prefer a paragraph break, then a sentence break, within a bounded
    lookback before `hard_end`; otherwise cut exactly at `hard_end`. Never
    looks earlier than `window_floor` (the chunk's own start), so a chunk is
    never snapped down to zero length.
    """
    window_start = max(window_floor, hard_end - _BOUNDARY_LOOKBACK_CHARS)
    window = text[window_start:hard_end]
    idx = window.rfind("\n\n")
    if idx != -1:
        return window_start + idx + 2
    idx = window.rfind(". ")
    if idx != -1:
        return window_start + idx + 2
    return hard_end


def chunk_document(
    parsed: ParsedDocument,
    *,
    chunk_size_chars: int,
    overlap_chars: int,
    header_footer_threshold: float,
    max_chunks: int | None = None,
) -> list[ChunkSpec]:
    """Split a parsed document into deterministic, bounded, overlapping
    chunks. Returns `[]` for a document whose text is empty or whitespace
    only (a valid, if unusual, outcome -- not an error at this layer).
    """
    pages = strip_repeated_headers_footers(parsed.pages, threshold=header_footer_threshold)
    full_text = "\n\n".join(page.text for page in pages)
    if not full_text.strip():
        return []

    page_boundaries = _page_boundaries(pages)
    headings = _heading_index(full_text)

    chunks: list[ChunkSpec] = []
    pos = 0
    text_length = len(full_text)
    while pos < text_length:
        hard_end = min(pos + chunk_size_chars, text_length)
        end = _snap_boundary(full_text, pos, hard_end) if hard_end < text_length else hard_end
        content = full_text[pos:end].strip()
        if content:
            char_start = pos
            char_end = end
            chunks.append(
                ChunkSpec(
                    ordinal=len(chunks),
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    char_start=char_start,
                    char_end=char_end,
                    page_start=_page_for_offset(page_boundaries, char_start),
                    page_end=_page_for_offset(page_boundaries, max(char_end - 1, char_start)),
                    heading_context=_heading_context_at(headings, char_start),
                )
            )
            if max_chunks is not None and len(chunks) > max_chunks:
                raise DocumentParseError(
                    "too_many_chunks",
                    f"Document produced more than {max_chunks} chunks",
                )
        next_pos = end - overlap_chars
        # Guarantees forward progress even if a snap-back plus overlap would
        # otherwise re-examine the same text forever.
        pos = next_pos if next_pos > pos else end

    return chunks
