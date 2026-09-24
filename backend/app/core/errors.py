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
