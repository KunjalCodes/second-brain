"""Deterministic entity names and input hygiene for project knowledge
(docs/PHASE16_ARCHITECTURE.md section 5).

Structural entities have EXACT identity, so their names are derived, never
guessed: `file:<path>`, `commit:<sha12>`, `session:<key>`. The prefix also keeps
entity types from colliding (a decision and a file can never share a normalised
name), and `EXACT_IDENTITY_TYPES` keeps them out of fuzzy duplicate flagging.

Everything an external agent or a Git collector sends is hostile until proven
otherwise: paths are normalised to a safe relative POSIX form (never absolute, never
`..`), and free text has control characters stripped and its length bounded.
"""

from __future__ import annotations

import re
import unicodedata

from app.core.errors import ValidationError

#: Entity names are capped at 200 characters and carry a `file:` prefix, so a path
#: longer than this cannot be represented as an entity. Such files are skipped by
#: ingestion and refused in events, rather than silently truncated.
MAX_PATH_LENGTH = 190
MAX_SEGMENT_LENGTH = 255
SHORT_SHA = 12
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")
#: Control characters and bidirectional overrides (filename/identifier spoofing).
#: Tab and newline are handled separately: kept in free text, forbidden in identifiers.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_IDENTIFIER_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def normalize_path(raw: str) -> str:
    """A repository-relative POSIX path, or `ValidationError`.

    Refused: absolute paths, drive letters, backslashes, `..` segments, NUL and
    control characters, over-long paths, and anything inside `.git/`. Accepted
    paths are NFC-normalised, with `./` and duplicate slashes removed.
    """
    if not isinstance(raw, str):
        raise ValidationError("path must be a string")
    value = unicodedata.normalize("NFC", raw.strip())
    if not value:
        raise ValidationError("path must not be empty")
    if len(value) > MAX_PATH_LENGTH:
        raise ValidationError(f"path must be at most {MAX_PATH_LENGTH} characters")
    if _IDENTIFIER_CONTROL.search(value) or "\\" in value:
        raise ValidationError("path contains forbidden characters")
    if value.startswith(("/", "~")) or _WINDOWS_DRIVE.match(value):
        raise ValidationError("path must be relative to the repository root")
    segments = [s for s in value.split("/") if s not in ("", ".")]
    if not segments:
        raise ValidationError("path must name a file or directory")
    if any(s == ".." or len(s) > MAX_SEGMENT_LENGTH for s in segments):
        raise ValidationError("path must not traverse outside the repository")
    if segments[0] == ".git":
        raise ValidationError("paths inside .git are not recorded")
    return "/".join(segments)


def normalize_sha(raw: str) -> str:
    value = raw.lower() if isinstance(raw, str) else ""
    if not _SHA_RE.fullmatch(value):
        raise ValidationError("commit must be a full 40- or 64-character hex hash")
    return value


def normalize_session_key(raw: str) -> str:
    if not isinstance(raw, str) or not _KEY_RE.fullmatch(raw):
        raise ValidationError(
            "session_id must be 1-64 characters: letters, digits and . _ : - "
            "(starting alphanumeric)"
        )
    return raw


def normalize_branch(raw: str) -> str:
    value = raw.strip() if isinstance(raw, str) else ""
    if (
        not value
        or len(value) > 255
        or _BRANCH_FORBIDDEN.search(value)
        or value.endswith(("/", "."))
    ):
        raise ValidationError("branch name is not a valid Git ref name")
    return value


def clean_text(raw: str, *, max_length: int, field: str = "text") -> str:
    """NFC, control and bidirectional-override characters removed, whitespace runs
    collapsed to single spaces (newlines kept as one `\\n` run), then bounded.
    Over-length input is REJECTED, not silently cut: a truncated decision could
    change its meaning."""
    if not isinstance(raw, str):
        raise ValidationError(f"{field} must be a string")
    value = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL.sub("", value)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n"))
    value = "\n".join(line for line in lines if line)
    if not value:
        raise ValidationError(f"{field} must not be empty")
    if len(value) > max_length:
        raise ValidationError(f"{field} must be at most {max_length} characters")
    return value


# -- entity names ---------------------------------------------------------------------------


def file_name(path: str) -> str:
    return f"file:{normalize_path(path)}"


def directory_name(path: str) -> str:
    return f"dir:{normalize_path(path)}"


def commit_name(sha: str) -> str:
    return f"commit:{normalize_sha(sha)[:SHORT_SHA]}"


def session_name(session_key: str) -> str:
    return f"session:{normalize_session_key(session_key)}"


def branch_name(branch: str) -> str:
    return f"branch:{normalize_branch(branch)}"


def symbol_name(path: str, qualified_name: str) -> str:
    qualified = clean_text(qualified_name, max_length=100, field="symbol")
    name = f"symbol:{normalize_path(path)}::{qualified}"
    if len(name) > 200:
        raise ValidationError("symbol name is too long to record")
    return name


def titled_name(kind: str, title: str) -> str:
    """`bug:<title>`, `decision:<title>`, ... with the title lower-cased for
    identity by `normalize_name`, so the same title always reaches the same entity
    (a FIX event can find the BUG it fixes)."""
    return f"{kind}:{clean_text(title, max_length=160, field='title')}"
