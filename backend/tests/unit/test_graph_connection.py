"""Connection layer: driver-error translation and credential hygiene.

No database is involved: the driver is replaced with a double that raises the
exceptions a real Neo4j would, so the mapping in ARCHITECTURE.md section 21 can be
verified for each failure class.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j import exceptions as neo4j_exceptions

from app.core.errors import (
    ConfigurationError,
    ConflictError,
    GraphQueryError,
    GraphUnavailableError,
)
from app.graph.connection import GraphClient, _safe_uri, get_graph_client, reset_graph_client


@pytest.fixture
def client() -> GraphClient:
    return GraphClient()


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (neo4j_exceptions.ServiceUnavailable("down"), GraphUnavailableError),
            (neo4j_exceptions.SessionExpired("expired"), GraphUnavailableError),
            (neo4j_exceptions.AuthError("bad creds"), ConfigurationError),
            (neo4j_exceptions.ConstraintError("duplicate"), ConflictError),
            (neo4j_exceptions.CypherSyntaxError("bad cypher"), GraphQueryError),
            (neo4j_exceptions.CypherTypeError("bad type"), GraphQueryError),
            (neo4j_exceptions.DatabaseError("boom"), GraphQueryError),
        ],
    )
    async def test_driver_errors_become_typed_application_errors(
        self, client: GraphClient, raised: Exception, expected: type[Exception]
    ) -> None:
        driver = AsyncMock()
        driver.execute_query.side_effect = raised
        client._driver = driver  # noqa: SLF001 - test double injection
        with pytest.raises(expected):
            await client.read("MATCH (n) RETURN n", {})

    async def test_unavailable_message_does_not_leak_internals(
        self, client: GraphClient
    ) -> None:
        driver = AsyncMock()
        driver.execute_query.side_effect = neo4j_exceptions.ServiceUnavailable(
            "bolt://secret-host:7687 refused"
        )
        client._driver = driver  # noqa: SLF001
        with pytest.raises(GraphUnavailableError) as exc:
            await client.read("MATCH (n) RETURN n", {})
        assert "secret-host" not in exc.value.message

    async def test_auth_failure_names_the_offending_variables(
        self, client: GraphClient
    ) -> None:
        driver = AsyncMock()
        driver.execute_query.side_effect = neo4j_exceptions.AuthError("nope")
        client._driver = driver  # noqa: SLF001
        with pytest.raises(ConfigurationError) as exc:
            await client.read("MATCH (n) RETURN n", {})
        assert "NEO4J_USERNAME" in exc.value.message


class TestUninitialisedDriver:
    async def test_read_without_a_driver_fails_loudly(self, client: GraphClient) -> None:
        with pytest.raises(GraphUnavailableError, match="not been initialised"):
            await client.read("MATCH (n) RETURN n", {})

    async def test_write_transaction_without_a_driver_fails_loudly(
        self, client: GraphClient
    ) -> None:
        with pytest.raises(GraphUnavailableError, match="not been initialised"):
            await client.write_transaction([("CREATE (n)", {})])

    async def test_verify_without_a_driver_fails_loudly(self, client: GraphClient) -> None:
        with pytest.raises(GraphUnavailableError):
            await client.verify()


class TestConnect:
    async def test_refuses_to_connect_without_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing configuration must fail with an actionable message, not a crash."""
        from app.core.config import Settings

        settings = Settings(neo4j_uri="", neo4j_username="", neo4j_password="")
        client = GraphClient(settings)
        with pytest.raises(ConfigurationError, match="NEO4J_URI"):
            await client.connect()

    async def test_close_is_safe_when_never_connected(self, client: GraphClient) -> None:
        await client.close()
        assert client.is_connected is False


class TestTransactionAtomicity:
    async def test_application_errors_propagate_unchanged(self, client: GraphClient) -> None:
        """A domain error raised inside the unit of work must abort the transaction.

        It must not be re-wrapped as a graph failure, or the service layer could not
        tell a rejected supersession from a database outage.
        """
        from app.core.errors import NotFoundError

        async def failing_execute_write(work: Any) -> Any:
            raise NotFoundError("missing")

        session = AsyncMock()
        session.execute_write = failing_execute_write
        # driver.session() is a *synchronous* call returning an async context
        # manager, so it must be a MagicMock rather than an AsyncMock.
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = session
        driver = MagicMock()
        driver.session = MagicMock(return_value=session_cm)
        client._driver = driver  # noqa: SLF001

        with pytest.raises(NotFoundError):
            await client.write_transaction([("CREATE (n)", {})])


class TestCredentialHygiene:
    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("bolt://localhost:7687", "bolt://localhost:7687"),
            ("neo4j+s://abc.databases.neo4j.io", "neo4j+s://abc.databases.neo4j.io"),
            ("neo4j+s://user:pw@abc.databases.neo4j.io", "neo4j+s://***@abc.databases.neo4j.io"),
        ],
    )
    def test_embedded_credentials_are_stripped_before_logging(
        self, uri: str, expected: str
    ) -> None:
        assert _safe_uri(uri) == expected
        assert "pw" not in _safe_uri(uri) or "pw" in expected


def test_client_singleton_is_reusable_and_resettable() -> None:
    reset_graph_client()
    first = get_graph_client()
    assert get_graph_client() is first
    reset_graph_client()
    assert get_graph_client() is not first
