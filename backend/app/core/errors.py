"""Application error taxonomy.

ARCHITECTURE.md section 21 requires that the backend distinguish error classes rather
than collapsing everything into "Something went wrong". Each error carries a stable
machine-readable `code` plus an HTTP status, so clients can branch on the code and
humans get an actionable message.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all errors this application raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"

    #: Extra response headers (e.g. `Retry-After`, `WWW-Authenticate`).
    headers: dict[str, str] | None = None
    #: Phase 9: the error handler clears the session cookie on the response.
    clear_session_cookie: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ValidationError(AppError):
    """Input that is syntactically valid but semantically rejected by domain rules."""

    status_code = 422
    code = "validation_error"


class NotFoundError(AppError):
    """A requested record does not exist *for the requesting user*.

    Cross-user access is reported as not-found rather than forbidden so that the API
    does not confirm the existence of another user's records.
    """

    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """The request contradicts the current state, e.g. an illegal status transition."""

    status_code = 409
    code = "conflict"


class GraphUnavailableError(AppError):
    """The graph database could not be reached or refused the connection."""

    status_code = 503
    code = "graph_unavailable"


class GraphQueryError(AppError):
    """The graph database rejected or failed a query."""

    status_code = 500
    code = "graph_query_error"


class DatabaseUnavailableError(AppError):
    """PostgreSQL could not be reached."""

    status_code = 503
    code = "database_unavailable"


class DatabaseError(AppError):
    """PostgreSQL rejected or failed a query."""

    status_code = 500
    code = "database_error"


class LLMUnavailableError(AppError):
    """The LLM provider is unreachable, rate-limiting, or erroring.

    Retryable. Extraction is asynchronous, so this never reaches a user request.
    """

    status_code = 503
    code = "llm_unavailable"


class LLMResponseInvalidError(AppError):
    """The provider replied, but the response did not satisfy the output schema.

    NOT blindly retryable: resending an identical request would most likely produce
    the same malformed output and burn tokens doing it.
    """

    status_code = 502
    code = "llm_response_invalid"


class ConfigurationError(AppError):
    """A required piece of configuration is missing or invalid."""

    status_code = 500
    code = "configuration_error"


class EmbeddingUnavailableError(AppError):
    """The embedding provider is unreachable, rate-limiting, or erroring.

    Retryable. Embedding is asynchronous (ADR 0009), so this never reaches a user
    request -- a memory is fully created and usable in Neo4j regardless of whether
    embedding succeeds.
    """

    status_code = 503
    code = "embedding_unavailable"


class VectorSearchUnavailableError(AppError):
    """The vector index/PostgreSQL could not be reached for a retrieval query.

    Distinct from `DatabaseUnavailableError` so `HybridRetrievalService` can catch
    it specifically and degrade to graph/keyword-only retrieval (PHASE3_PLAN.md
    section 4/14, core invariant 6) rather than failing the whole request.
    """

    status_code = 503
    code = "vector_search_unavailable"


class UnauthorizedError(AppError):
    """The caller did not present valid credentials for the endpoint.

    Used only by internal, non-user-scoped operations that are not part of the
    public API surface (e.g. the sweep trigger) -- `X-User-Id` handling has its own
    error paths (`ValidationError`, `ConfigurationError`) and is unaffected.
    """

    status_code = 401
    code = "unauthorized"


class StorageUnavailableError(AppError):
    """Document byte storage (Phase 6, PHASE6_PLAN.md section 10) could not
    be written to or read from -- e.g. the local filesystem's configured
    root directory is not writable. Distinct from `DatabaseUnavailableError`/
    `GraphUnavailableError` so callers can tell which of the three stores
    failed.
    """

    status_code = 503
    code = "storage_unavailable"


# -- Phase 9: authentication, authorization boundary, abuse control ---------------


class AuthenticationRequiredError(UnauthorizedError):
    """No valid user credential: missing, malformed, unknown, expired, idle,
    revoked, or its account is disabled. Deliberately ONE generic code and
    message -- the specific cause is logged server-side, never returned
    (docs/PHASE9_PLAN.md section 13)."""

    code = "authentication_required"

    def __init__(self, *, clear_cookie: bool = False) -> None:
        super().__init__("Authentication is required")
        self.headers = {"WWW-Authenticate": 'Bearer realm="api"'}
        self.clear_session_cookie = clear_cookie


class InvalidCredentialsError(UnauthorizedError):
    """Login or password change with a wrong password -- or an unknown email,
    which is indistinguishable by design."""

    code = "invalid_credentials"

    def __init__(self) -> None:
        super().__init__("Invalid email or password")


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class AccountDisabledError(ForbiddenError):
    """Returned only after a CORRECT password, so it reveals nothing to a
    caller who does not already hold the credential."""

    code = "account_disabled"

    def __init__(self) -> None:
        super().__init__("This account is disabled")


class OriginNotAllowedError(ForbiddenError):
    """A cookie-authenticated state-changing request (or a login/register)
    whose `Origin` is missing or not allowed -- the CSRF boundary."""

    code = "origin_not_allowed"

    def __init__(self) -> None:
        super().__init__("Request origin is not allowed")


class EmailTakenError(ConflictError):
    code = "email_taken"

    def __init__(self) -> None:
        super().__init__("An account with this email already exists")


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, *, limit_class: str, retry_after_seconds: int) -> None:
        super().__init__(
            "Too many requests; retry later",
            details={"limit_class": limit_class, "retry_after_seconds": retry_after_seconds},
        )
        self.headers = {"Retry-After": str(retry_after_seconds)}
