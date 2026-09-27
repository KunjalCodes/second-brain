"""Logging setup (Phase 11, M5; docs/PHASE11_PLAN.md section 9).

Managed hosts collect stdout, so plain stream logging is sufficient and avoids any
dependency on local filesystem state.

* `LOG_FORMAT=json` (the default in production-like environments) emits one
  JSON object per line; `text` keeps the human-readable development format.
* Correlation fields -- `request_id`, `user_id`, `job_id`, `job_kind` -- live in
  context variables set by the request middleware, the auth dependency and the
  job runners, and are attached to EVERY record emitted while they are set.
* Structured extras: a call may pass `extra={...}` with any of `EXTRA_FIELDS`
  (route, status, latency_ms, provider, model, retry_count, ...); unknown keys
  are ignored so nothing unexpected is serialized.

What is never logged is enforced by callers, not by this module, and pinned by
`tests/unit/test_log_redaction.py`: no passwords, session tokens, cookies,
Authorization headers, API keys, request/response bodies, search text,
document content or email addresses.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)
_job_kind: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_kind", default=None)

#: `extra=` keys copied into JSON output (anything else is dropped).
EXTRA_FIELDS = (
    "event",
    "route",
    "method",
    "status",
    "latency_ms",
    "client_request_id",
    "provider",
    "model",
    "retry_count",
    "attempt",
    "error_code",
    "state",
)


def bind_request(request_id: str | None) -> contextvars.Token:
    return _request_id.set(request_id)


def reset_request(token: contextvars.Token) -> None:
    _request_id.reset(token)


def bind_user(user_id: str | None) -> None:
    """Called by the auth dependency once identity is resolved. Scoped to the
    current request's context (each request runs in its own context copy)."""
    _user_id.set(user_id)


def bind_job(job_id: str | None, kind: str | None) -> tuple[contextvars.Token, contextvars.Token]:
    return _job_id.set(job_id), _job_kind.set(kind)


def reset_job(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    _job_id.reset(tokens[0])
    _job_kind.reset(tokens[1])


def current_request_id() -> str | None:
    return _request_id.get()


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        record.user_id = _user_id.get()
        record.job_id = _job_id.get()
        record.job_kind = _job_kind.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "user_id", "job_id", "job_kind"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key in EXTRA_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info:
            # Tracebacks stay in the SERVER log (never in responses).
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s | %(message)s%(ctx)s")

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"{key}={getattr(record, key)}"
            for key in ("request_id", "job_id")
            if getattr(record, key, None)
        ]
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return super().format(record)


#: Library loggers that write query TEXT, PARAMETERS or full URLs -- i.e. user
#: content such as search terms, or credentials. Pinned to WARNING on every configuration path,
#: whatever the root level (found by tests/integration/test_log_redaction.py).
_QUERY_LOGGERS = (
    "neo4j",
    "neo4j.io",
    "neo4j.pool",
    "neo4j.notifications",
    "sqlalchemy.engine",
    # httpx logs every request URL at INFO -- including query strings, and for
    # outbound provider calls possibly an API key passed as a query parameter.
    "httpx",
    "httpcore",
)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    for name in _QUERY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    root = logging.getLogger()
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    ours = [h for h in root.handlers if getattr(h, "_second_brain", False)]
    if ours:
        for handler in ours:
            handler.setFormatter(formatter)
        root.setLevel(level)
        return
    if root.handlers:
        # A host/test harness installed handlers already: keep them, but make
        # sure correlation fields are available to their formatters.
        for handler in root.handlers:
            if not any(isinstance(f, _ContextFilter) for f in handler.filters):
                handler.addFilter(_ContextFilter())
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler._second_brain = True  # type: ignore[attr-defined]
    handler.addFilter(_ContextFilter())
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)
    # Our own access line (app.access) replaces uvicorn's, which records raw
    # query strings -- i.e. search text (Phase 11, M5).
    logging.getLogger("uvicorn.access").disabled = True
