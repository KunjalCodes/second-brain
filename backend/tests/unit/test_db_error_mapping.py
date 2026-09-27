"""Phase 11 (M13 drill finding): connection-level PostgreSQL failures are 503,
not an opaque 500. The outage drill got `internal_error` because asyncpg's
connect-time errors reached the handler unwrapped by SQLAlchemy."""

from __future__ import annotations

import asyncpg.exceptions as pg_errors
import pytest

from app.core.errors import DatabaseUnavailableError
from app.db.engine import map_database_error


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(61, "Connect call failed"),
        ConnectionResetError(54, "Connection reset by peer"),
        OSError("Network is unreachable"),
        TimeoutError(),
        pg_errors.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
        pg_errors.TargetServerAttributeNotMatched("no server matched"),
        pg_errors.InterfaceError("cannot perform operation: another operation is in progress"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_connection_failures_map_to_unavailable(exc: Exception) -> None:
    mapped = map_database_error(exc)
    assert isinstance(mapped, DatabaseUnavailableError)
    assert mapped.status_code == 503


def test_unrelated_errors_are_left_alone() -> None:
    err = ValueError("not a database problem")
    assert map_database_error(err) is err
