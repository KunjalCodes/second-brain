"""Opaque session tokens (ADR 0020): 256 bits from `secrets`, prefixed so
secret scanners can recognise a leaked one. Only the SHA-256 hex digest is
ever stored; a fast hash is correct for a high-entropy random secret."""

from __future__ import annotations

import hashlib
import re
import secrets

TOKEN_PREFIX = "sbs_"
#: `sbs_` + token_urlsafe(32) (43 chars). Anything else is rejected before lookup.
TOKEN_RE = re.compile(r"^sbs_[A-Za-z0-9_-]{43}$")


def new_session_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_well_formed(token: str) -> bool:
    return bool(TOKEN_RE.fullmatch(token))


# -- Phase 16: project tokens (docs/PHASE16_ARCHITECTURE.md section 9) -------------
# A DIFFERENT prefix from session tokens on purpose: a project token can never be
# mistaken for (or accepted as) a session, and secret scanners can tell them apart.

PROJECT_TOKEN_PREFIX = "sbp_"
PROJECT_TOKEN_RE = re.compile(r"^sbp_[A-Za-z0-9_-]{43}$")


def new_project_token() -> str:
    return PROJECT_TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_project_token(token: str) -> bool:
    return token.startswith(PROJECT_TOKEN_PREFIX)


def is_well_formed_project_token(token: str) -> bool:
    return bool(PROJECT_TOKEN_RE.fullmatch(token))
