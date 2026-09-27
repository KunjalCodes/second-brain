"""Neo4j connection layer.

This is the only module that talks to the driver. Everything above it sees plain
dicts and application errors, which keeps the rest of the codebase independent of
the driver version and makes the store replaceable.

Credentials come exclusively from the environment (see app.core.config); there are no
defaults and nothing localhost-specific, so the same code runs against a local Neo4j
and against a managed Neo4j Aura instance (`neo4j+s://...`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, RoutingControl
from neo4j import exceptions as neo4j_exceptions

from app.core.config import Settings, get_settings
from app.core.errors import (
    AppError,
    ConfigurationError,
    ConflictError,
    GraphQueryError,
    GraphUnavailableError,
)
from app.graph.serialization import records_to_dicts

logger = logging.getLogger(__name__)


def _map_driver_error(exc: Exception, query: str) -> Exception:
    """Translate a driver exception into the application's error taxonomy.

    ARCHITECTURE.md section 21 requires graph failures to be distinguishable from
    validation, authorization and query failures. The Cypher text is logged but never
    returned to the client.
    """
    if isinstance(exc, neo4j_exceptions.AuthError):
        logger.error("Neo4j rejected the supplied credentials")
        return ConfigurationError(
            "The graph database rejected the configured credentials. "
            "Check NEO4J_USERNAME and NEO4J_PASSWORD."
        )
    if isinstance(exc, neo4j_exceptions.ConstraintError):
        return ConflictError("The write violates a graph uniqueness constraint")
    if isinstance(
        exc,
        neo4j_exceptions.ServiceUnavailable
        | neo4j_exceptions.SessionExpired
        | neo4j_exceptions.TransientError
        | neo4j_exceptions.ConnectionAcquisitionTimeoutError,
    ):
        logger.error("Graph database unavailable: %s", exc)
        return GraphUnavailableError(
            "The graph database is currently unreachable. Please retry shortly."
        )
    if isinstance(exc, neo4j_exceptions.ConfigurationError):
        logger.error("Neo4j driver configuration error: %s", exc)
        return ConfigurationError("Invalid graph database configuration")
    if isinstance(exc, neo4j_exceptions.CypherSyntaxError | neo4j_exceptions.CypherTypeError):
        # A programming error on our side: log the statement, return a generic message.
        logger.exception("Invalid Cypher submitted: %s", query)
        return GraphQueryError("The graph query was rejected by the database")
    if isinstance(exc, neo4j_exceptions.Neo4jError | neo4j_exceptions.DriverError):
        logger.exception("Graph query failed: %s", query)
        return GraphQueryError("The graph query failed")
    return exc


@dataclass(frozen=True)
class TxStep:
    """One statement inside a multi-statement transaction.

    `check` is called with the statement's rows *while the transaction is still
    open*. Raising from it aborts and rolls the whole transaction back, which is how
    a write is rejected without leaving a partial record behind. Validating after
    `write_transaction` returns would be too late: the transaction has committed by
    then.
    """

    query: str
    params: dict[str, Any] = field(default_factory=dict)
    check: Callable[[list[dict[str, Any]]], None] | None = None


class GraphClient:
    """Thin async wrapper around the Neo4j driver."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._driver: AsyncDriver | None = None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Create the driver. Does not open a connection (the pool is lazy)."""
        if self._driver is not None:
            return
        s = self._settings
        if not s.neo4j_configured:
            raise ConfigurationError(
                "Neo4j is not configured. Set NEO4J_URI, NEO4J_USERNAME and "
                "NEO4J_PASSWORD (see .env.example)."
            )
        try:
            self._driver = AsyncGraphDatabase.driver(
                s.neo4j_uri,
                auth=(s.neo4j_username, s.neo4j_password),
                max_connection_pool_size=s.neo4j_max_connection_pool_size,
                connection_acquisition_timeout=s.neo4j_connection_timeout,
                max_transaction_retry_time=s.neo4j_max_transaction_retry_time,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as an application error
            raise _map_driver_error(exc, "<driver init>") from exc
        logger.info(
            "Neo4j driver created for %s (database=%s)",
            _safe_uri(s.neo4j_uri),
            s.neo4j_database,
        )

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
            logger.info("Neo4j driver closed")

    @property
    def is_connected(self) -> bool:
        return self._driver is not None

    async def verify(self) -> None:
        """Actively check that the database answers. Raises an application error."""
        if self._driver is None:
            raise GraphUnavailableError("The graph driver has not been initialised")
        try:
            await self._driver.verify_connectivity()
        except Exception as exc:  # noqa: BLE001
            raise _map_driver_error(exc, "<verify_connectivity>") from exc

    # -- queries -----------------------------------------------------------

    async def read(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self._execute(query, params, RoutingControl.READ)

    async def write(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self._execute(query, params, RoutingControl.WRITE)

    async def write_transaction(
        self,
        steps: Sequence[TxStep],
    ) -> list[list[dict[str, Any]]]:
        """Run several statements in ONE transaction, returning each statement's rows.

        Creating a memory writes a Memory node, entity nodes, MENTIONS edges, a Source
        node and RELATES_TO edges. Those must commit together: a partially-written
        memory is corrupt memory, and there is no cross-statement rollback if each
        statement is sent separately.

        A step's `check` runs inside the transaction, so raising from it rolls
        everything back -- that is how a supersession naming a memory that does not
        exist is rejected without leaving orphan nodes in the graph.
        """
        if self._driver is None:
            raise GraphUnavailableError(
                "The graph driver has not been initialised. This usually means the "
                "application failed to start up correctly."
            )

        async def unit_of_work(tx: Any) -> list[list[dict[str, Any]]]:
            collected: list[list[dict[str, Any]]] = []
            for step in steps:
                result = await tx.run(step.query, step.params)  # type: ignore[arg-type]
                rows = records_to_dicts([r async for r in result])
                if step.check is not None:
                    # Raising here aborts the transaction before it commits.
                    step.check(rows)
                collected.append(rows)
            return collected

        try:
            async with self._driver.session(database=self._settings.neo4j_database) as session:
                return await session.execute_write(unit_of_work)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_driver_error(exc, "<write transaction>") from exc

    async def _execute(
        self,
        query: str,
        params: dict[str, Any] | None,
        routing: RoutingControl,
    ) -> list[dict[str, Any]]:
        if self._driver is None:
            raise GraphUnavailableError(
                "The graph driver has not been initialised. This usually means the "
                "application failed to start up correctly."
            )
        try:
            result = await self._driver.execute_query(
                query,  # type: ignore[arg-type]  # runtime-built Cypher, see repositories
                params or {},
                routing_=routing,
                database_=self._settings.neo4j_database,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_driver_error(exc, query) from exc
        return records_to_dicts(list(result.records))


def _safe_uri(uri: str) -> str:
    """Strip any embedded credentials before logging a URI."""
    if "@" in uri:
        scheme, _, rest = uri.partition("://")
        return f"{scheme}://***@{rest.rpartition('@')[2]}"
    return uri


# -- process-wide instance -------------------------------------------------

_client: GraphClient | None = None


def get_graph_client() -> GraphClient:
    """Return the process-wide graph client, creating the object if needed."""
    global _client
    if _client is None:
        _client = GraphClient()
    return _client


def reset_graph_client() -> None:
    """Drop the process-wide client. Used by tests."""
    global _client
    _client = None
