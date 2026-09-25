"""Password hashing with the standard library's scrypt (ADR 0020).

Stored format: `scrypt$<N>$<r>$<p>$<salt_b64>$<hash_b64>`. Parameters travel
with each hash, so raising the configured cost re-hashes on the next login
(`needs_rehash`) without breaking existing accounts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from app.core.errors import ValidationError

SALT_BYTES = 16
KEY_BYTES = 32
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_CHARS = 128
EMAIL_MAX_CHARS = 254
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")


@dataclass(frozen=True)
class ScryptParams:
    n: int
    r: int
    p: int
    maxmem: int


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    maxmem = max(params.maxmem, 128 * params.n * params.r * 2)
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=maxmem,
        dklen=KEY_BYTES,
    )


def hash_password(password: str, params: ScryptParams) -> str:
    salt = os.urandom(SALT_BYTES)
    digest = _derive(password, salt, params)
    return f"scrypt${params.n}${params.r}${params.p}${_b64(salt)}${_b64(digest)}"


def _parse(stored: str) -> tuple[ScryptParams, bytes, bytes] | None:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except ValueError:
        return None
    return ScryptParams(n=n, r=r, p=p, maxmem=0), salt, digest


def verify_password(password: str, stored: str, *, maxmem: int) -> bool:
    """Constant-time comparison of the derived key. A malformed stored hash
    verifies as False, never raises."""
    parsed = _parse(stored)
    if parsed is None:
        return False
    params, salt, expected = parsed
    actual = _derive(password, salt, ScryptParams(params.n, params.r, params.p, maxmem))
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str, params: ScryptParams) -> bool:
    parsed = _parse(stored)
    if parsed is None:
        return True
    old = parsed[0]
    return (old.n, old.r, old.p) != (params.n, params.r, params.p)


def normalize_email(email: str) -> str:
    """Trim + lowercase + a basic `local@domain` check. Raises ValidationError."""
    value = email.strip().lower()
    if len(value) > EMAIL_MAX_CHARS or not _EMAIL_RE.match(value):
        raise ValidationError("A valid email address is required")
    return value


def check_password_policy(password: str, *, email_normalized: str) -> None:
    """Length 12-128 characters, not the email itself (NIST SP 800-63B style).
    No composition rules; no network breach check (offline, zero-cost)."""
    if len(password) < PASSWORD_MIN_CHARS or len(password) > PASSWORD_MAX_CHARS:
        raise ValidationError(
            f"Password must be {PASSWORD_MIN_CHARS}-{PASSWORD_MAX_CHARS} characters long"
        )
    if password.strip().lower() == email_normalized:
        raise ValidationError("Password must not be the email address")
