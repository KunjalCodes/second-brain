"""Shared Gemini SDK error mapping for the LLM and embedding adapters
(Phase 11, M3). One attempt's failure -> the application taxonomy, including
the provider's Retry-After hint on HTTP 429 and transport-level errors that
previously bypassed both the retry loop and the mapping.

User-facing messages never contain SDK exception text (docs/PHASE11_PLAN.md
section 11); the original exception is chained (`__cause__`) and logged.
"""

from __future__ import annotations

import re
from typing import Any

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def retry_after_seconds(exc: Any) -> float | None:  # noqa: ANN401 - SDK exception
    """Best-effort extraction of the provider's retry hint: an HTTP
    `Retry-After` header (seconds), or Google RPC `RetryInfo.retryDelay`
    ("13s") in the error details. None when absent or unparseable."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("retry-after") if hasattr(headers, "get") else None
        if value is not None:
            try:
                seconds = float(value)
                if seconds >= 0:
                    return seconds
            except (TypeError, ValueError):
                pass
    details = getattr(exc, "details", None)
    error = details.get("error") if isinstance(details, dict) else None
    items = error.get("details") if isinstance(error, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            match = _DURATION.match(str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def is_transport_error(exc: BaseException) -> bool:
    """Connection-level failures (DNS, refused, reset, read errors)."""
    try:
        import httpx

        if isinstance(exc, httpx.TransportError):
            return True
    except ImportError:  # pragma: no cover - httpx is a google-genai dependency
        pass
    return isinstance(exc, ConnectionError | OSError)
