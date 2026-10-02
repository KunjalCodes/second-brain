"""Opaque pagination cursors (docs/PHASE16_ARCHITECTURE.md section 8).

A cursor is a keyset position `(valid time, id)` inside ONE project's already
user-scoped result set: it names where to resume, not what may be read, so forging
one can only move within data the caller is already authorised to see. Malformed
cursors are a clean 422, never an exception.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime

from app.core.errors import ValidationError

MAX_CURSOR_LENGTH = 256


def encode_cursor(at: datetime, row_id: str) -> str:
    raw = json.dumps({"a": at.astimezone(UTC).isoformat(), "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValidationError("cursor is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        at = datetime.fromisoformat(data["a"])
        row_id = data["i"]
        if at.tzinfo is None or not isinstance(row_id, str) or len(row_id) > 64:
            raise ValueError("shape")
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ValidationError("cursor is invalid") from None
    return at, row_id
