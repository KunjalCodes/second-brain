"""Neo4j connectivity and schema application against a real database."""

from __future__ import annotations

import pytest

from app.graph.connection import GraphClient
from app.graph.schema import CONSTRAINTS, INDEXES, SHOW_CONSTRAINTS, SHOW_INDEXES

pytestmark = pytest.mark.integration


async def test_driver_connects_and_verifies(graph: GraphClient) -> None:
    await graph.verify()
    assert graph.is_connected is True


async def test_simple_round_trip(graph: GraphClient) -> None:
    rows = await graph.read("RETURN 1 AS one")
    assert rows == [{"one": 1}]


async def test_parameters_are_passed_through(graph: GraphClient) -> None:
    rows = await graph.read("RETURN $value AS echoed", {"value": "hello"})
    assert rows[0]["echoed"] == "hello"


async def test_datetimes_round_trip_as_aware_utc(graph: GraphClient) -> None:
    """Validity windows are compared in Cypher, so timestamps must survive intact."""
    from datetime import UTC, datetime

    moment = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
    rows = await graph.read("RETURN $moment AS moment", {"moment": moment})
    returned = rows[0]["moment"]
    assert returned == moment
    assert returned.tzinfo is not None


async def test_all_schema_statements_apply_cleanly(graph: GraphClient) -> None:
    """The `graph` fixture applies the schema; assert everything landed.

    This is what proves the schema uses only Community-Edition features.
    """
    constraint_names = {row["name"] for row in await graph.read(SHOW_CONSTRAINTS)}
    index_names = {row["name"] for row in await graph.read(SHOW_INDEXES)}

    for statement in CONSTRAINTS:
        assert statement.split()[2] in constraint_names, statement
    for statement in INDEXES:
        assert statement.split()[2] in index_names, statement


async def test_schema_application_is_idempotent(graph: GraphClient) -> None:
    from app.graph.schema import SCHEMA_STATEMENTS

    for statement in SCHEMA_STATEMENTS:
        await graph.write(statement)
        await graph.write(statement)


async def test_entity_identity_constraint_blocks_duplicates(
    graph: GraphClient, user_id: str, cleanup: None
) -> None:
    """The composite constraint is what makes entity MERGE safe under concurrency."""
    from app.core.errors import ConflictError

    await graph.write(
        "CREATE (e:Entity {id: $id, user_id: $user_id, normalized_name: 'dup'})",
        {"id": f"{user_id}-1", "user_id": user_id},
    )
    with pytest.raises(ConflictError):
        await graph.write(
            "CREATE (e:Entity {id: $id, user_id: $user_id, normalized_name: 'dup'})",
            {"id": f"{user_id}-2", "user_id": user_id},
        )


async def test_same_entity_name_allowed_for_different_users(
    graph: GraphClient, user_id: str, other_user_id: str, cleanup: None
) -> None:
    """Uniqueness is per user: two users may each have a "Drawing Extractor"."""
    for uid in (user_id, other_user_id):
        await graph.write(
            "CREATE (e:Entity {id: $id, user_id: $user_id, "
            "normalized_name: 'drawing extractor'})",
            {"id": f"{uid}-e", "user_id": uid},
        )
    rows = await graph.read(
        "MATCH (e:Entity {normalized_name: 'drawing extractor'}) "
        "WHERE e.user_id IN $uids RETURN count(e) AS n",
        {"uids": [user_id, other_user_id]},
    )
    assert rows[0]["n"] == 2
