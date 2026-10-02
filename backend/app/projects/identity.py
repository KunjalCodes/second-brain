"""Project identity (docs/PHASE16_ARCHITECTURE.md section 4).

A project is identified by WHAT the repository is, never by WHERE it lives on
someone's disk. Two derivations, in order of preference:

1. `remote_url`: the normalised `host/owner/repo` of a remote. Credentials,
   scheme, port, a trailing `.git` and case are all discarded, so `https`,
   `ssh` and scp-style spellings of one repository collapse to one identity.
2. `root_commits`: the sorted, de-duplicated root commit hash(es), for a
   repository with no remote.

The identity key is a salted SHA-256 hex of that canonical string. It is
stored and compared; the raw URL or hashes are never needed again.

Filesystem paths are REFUSED as repository identifiers (`/`, `~`, `.`-relative,
`file://`, Windows drives): a path is neither stable across machines nor safe to
expose, and accepting one would invite exactly the leak the spec forbids.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from app.core.errors import ValidationError

IdentityKind = Literal["remote_url", "root_commits"]

MAX_URL_LENGTH = 300
MAX_ROOT_COMMITS = 8
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCHEME_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.+)$", re.IGNORECASE)
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")
_ALLOWED_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh", "ssh+git"})
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class ProjectIdentity:
    kind: IdentityKind
    #: SHA-256 hex (64 chars) of the namespaced canonical string.
    key: str
    #: The canonical string a person can read in a log or doc. For `remote_url` it
    #: is `host/owner/repo`; for `root_commits` it is `root:<short hashes>`.
    display: str


def _reject(message: str) -> ValidationError:
    return ValidationError(message)


def normalize_repository_url(raw: str) -> str:
    """Canonical `host/owner/repo` (lowercase). Raises `ValidationError` for
    anything that is not a plausible remote URL -- including every filesystem path."""
    if not isinstance(raw, str):
        raise _reject("repository_url must be a string")
    value = raw.strip()
    if not value:
        raise _reject("repository_url must not be empty")
    if len(value) > MAX_URL_LENGTH:
        raise _reject(f"repository_url must be at most {MAX_URL_LENGTH} characters")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in value):
        raise _reject("repository_url must not contain whitespace or control characters")
    if (
        value.startswith(("/", "~", ".", "\\"))
        or value.lower().startswith("file:")
        or _WINDOWS_DRIVE_RE.match(value)
    ):
        raise _reject("repository_url must be a remote URL, not a filesystem path")

    scheme_match = _SCHEME_RE.match(value)
    if scheme_match:
        if scheme_match["scheme"].lower() not in _ALLOWED_SCHEMES:
            raise _reject("repository_url scheme is not supported")
        rest = scheme_match["rest"]
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]  # drop any user[:password]@
        host = host.rpartition(":")[0] if ":" in host and not host.startswith("[") else host
    else:
        scp = _SCP_RE.match(value)
        if not scp:
            raise _reject("repository_url is not a recognisable remote URL")
        host, path = scp["host"], scp["path"]

    host = host.lower().strip(".")
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    segments = [s for s in path.lower().split("/") if s]
    if not host or not _HOST_RE.match(host) or "." not in host and host != "localhost":
        raise _reject("repository_url has no valid host")
    if len(segments) < 2:
        raise _reject("repository_url must include an owner and a repository name")
    if len(segments) > 6 or any(s in {".", ".."} or not _SEGMENT_RE.match(s) for s in segments):
        raise _reject("repository_url has an invalid path")
    return f"{host}/{'/'.join(segments)}"


def normalize_root_commits(raw: list[str]) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise _reject("root_commits must be a non-empty list")
    cleaned = {c.strip().lower() for c in raw if isinstance(c, str)}
    if len(cleaned) > MAX_ROOT_COMMITS:
        raise _reject(f"root_commits accepts at most {MAX_ROOT_COMMITS} hashes")
    if not cleaned or any(not _SHA_RE.match(c) for c in cleaned):
        raise _reject("root_commits must be full 40- or 64-character hex commit hashes")
    return sorted(cleaned)


def _key(namespace: str, canonical: str) -> str:
    return hashlib.sha256(f"ai-second-brain:project:{namespace}:{canonical}".encode()).hexdigest()


def derive_identity(
    *, repository_url: str | None, root_commits: list[str] | None
) -> ProjectIdentity:
    """Remote URL wins when both are supplied. Exactly one of them is required."""
    if repository_url:
        canonical = normalize_repository_url(repository_url)
        return ProjectIdentity("remote_url", _key("v1:url", canonical), canonical)
    if root_commits:
        hashes = normalize_root_commits(root_commits)
        return ProjectIdentity(
            "root_commits",
            _key("v1:root", ",".join(hashes)),
            "root:" + ",".join(h[:12] for h in hashes),
        )
    raise _reject("Provide repository_url or root_commits")
