"""Every audited graph statement parses under BOTH Cypher dialects.

Local development runs Neo4j with `db.query.default_language=CYPHER_25`, but
AuraDB defaults to Cypher 5 (docs/DEPLOYMENT.md). A statement that only parses
under 25 would pass every other test and fail on the first staging request.
`EXPLAIN` plans without executing, so no data is read or written.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.repositories import entity_repository, graph_repository, memory_repository

pytestmark = pytest.mark.integration

STATEMENTS = [
    (module.__name__.rsplit(".", 1)[-1], index, statement)
    for module in (memory_repository, entity_repository, graph_repository)
    for index, statement in enumerate(module.USER_SCOPED_STATEMENTS)
]


def test_the_audit_lists_are_not_vacuous() -> None:
    assert len(STATEMENTS) >= 40


@pytest.mark.parametrize("dialect", ["CYPHER 5", "CYPHER 25"])
@pytest.mark.parametrize(("module", "index", "statement"), STATEMENTS, ids=lambda v: str(v)[:24])
async def test_statement_parses(
    graph, dialect: str, module: str, index: int, statement: str
) -> None:  # noqa: ANN001
    if statement == memory_repository.SEARCH_MEMORIES_BASE:
        # A prefix: the repository appends filter fragments and a RETURN clause.
        statement = f"{statement}{memory_repository.SEARCH_MEMORIES_RETURN}"
    if "{hops}" in statement:  # a template: the repository formats a validated hop count in
        statement = statement.format(hops=3)
    async with graph._driver.session(database=get_settings().neo4j_database) as session:  # noqa: SLF001
        result = await session.run(f"{dialect} EXPLAIN {statement}", {})
        await result.consume()
