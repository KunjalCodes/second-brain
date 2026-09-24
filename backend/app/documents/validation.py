"""Upload validation (Phase 6, PHASE6_PLAN.md section 1/6/9).

Content is sniffed from the actual bytes, never trusted from the client's
declared `Content-Type` header or the filename's extension -- the one
narrow, deliberate exception is documented in `sniff_content_type` below.
Document contents are untrusted data at every step here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.documents.parsers import SUPPORTED_CONTENT_TYPES
from app.documents.parsers.base import DocumentParseError

#: A generous but bounded display filename -- long enough for any real
#: filename, short enough that it can never be used to smuggle something
#: absurd into a UI or a log line.
_MAX_DISPLAY_FILENAME_LENGTH = 255

#: Characters allowed in the *displayed* filename after sanitization.
#: Deliberately narrow -- this is display-only metadata (the real storage
#: key is always a generated UUID, `app/storage/local.py`), but a
#: sanitized display value is still worth having so a crafted filename can
#: never inject control characters or path-shaped strings into logs or a
#: future UI.
_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


@dataclass(frozen=True)
class UploadValidation:
    content_type: str
    sanitized_filename: str


def sanitize_filename(raw_filename: str) -> str:
    """Display-only. Never used to build a storage path (section 9) -- the
    real storage key is always a generated UUID (`app/storage/local.py`).
    Strips control characters and any path separator, keeps only the final
    path component (defeats a filename like `"../../etc/passwd"` even
    before it reaches anything that might naively join it into a path),
    and bounds the length.
    """
    base_name = PurePosixPath(raw_filename.replace("\\", "/")).name or "untitled"
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base_name).strip()
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:_MAX_DISPLAY_FILENAME_LENGTH]


def sniff_content_type(data: bytes, *, filename: str) -> str:
    """Determine the real content type from the bytes themselves.

    PDF is detected by its magic header (`%PDF-`) -- a reliable,
    content-only signal. TXT and Markdown are, at the byte level, the exact
    same format (Markdown is plain text with a documented convention) --
    there is no content-only signal that distinguishes them the way a magic
    header distinguishes PDF. The file extension is used *only* to choose
    which of these two identical-format labels to apply (both are parsed
    identically, `app/documents/parsers/text.py`) -- it is never used to
    decide whether the file is safe or parseable; that decision is the
    UTF-8/null-byte check below, which runs regardless of the extension.
    """
    if data.startswith(b"%PDF-"):
        return "application/pdf"

    if b"\x00" in data:
        raise DocumentParseError(
            "unsupported_mime",
            "The uploaded file's content could not be identified as one of the "
            "supported formats (TXT, Markdown, or text-layer PDF)",
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentParseError(
            "unsupported_mime",
            "The uploaded file's content could not be identified as one of the "
            "supported formats (TXT, Markdown, or text-layer PDF)",
        ) from exc

    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in (".md", ".markdown"):
        return "text/markdown"
    return "text/plain"


def validate_upload(
    data: bytes, *, filename: str, max_size_bytes: int
) -> UploadValidation:
    """The one entry point the upload endpoint calls. Raises
    `DocumentParseError` (never a bare exception) for every rejection
    reason, each with a stable `error_code` recorded on the `Document` row.
    """
    if len(data) == 0:
        raise DocumentParseError("empty_document", "The uploaded file is empty")
    if len(data) > max_size_bytes:
        raise DocumentParseError(
            "file_too_large",
            f"The uploaded file is {len(data)} bytes, exceeding the limit of "
            f"{max_size_bytes} bytes",
        )

    content_type = sniff_content_type(data, filename=filename)
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise DocumentParseError(
            "unsupported_mime", f"Content type {content_type!r} is not supported"
        )

    return UploadValidation(
        content_type=content_type, sanitized_filename=sanitize_filename(filename)
    )
