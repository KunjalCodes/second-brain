"""Static audit of every Cypher statement in the repository layer.

Multi-user isolation (ARCHITECTURE.md section 15) is enforced by one rule: every
statement filters on `$user_id`. That rule is cheap to state and easy to break, and
breaking it leaks one user's memory to another. These tests check it by inspection,
so the failure shows up in CI without a database -- the Cypher itself is exercised
against real Neo4j by the integration suite.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import pytest

import app.repositories as repositories_package
from app.repositories import entity_repository, graph_repository, memory_repository
from app.repositories.memory_repository import (
    FILTER_FRAGMENTS,
    SEARCH_MEMORIES_BASE,
    SEARCH_MEMORIES_RETURN,
    _build_filters,
)

#: Constants that are query *fragments* rather than whole statements. They are
#: concatenated onto a base statement that carries the user filter.
FRAGMENT_NAMES = {
    "SEARCH_MEMORIES_RETURN",
    "COUNT_MEMORIES_RETURN",
    "SHOW_CONSTRAINTS",
    "SHOW_INDEXES",
    # graph_repository's shared WHERE predicate, joined into whole statements
    # at import time; every statement containing it is audited on its own.
    "_EDGE_IN_SCOPE",
    "_ASSERTING_MEMORY_IN_SCOPE",
}

READ_WRITE_KEYWORDS = re.compile(r"\b(MATCH|CREATE|MERGE)\b")

#: Every repository module containing Cypher. The meta-test below fails if a
#: module under app/repositories/ holds Cypher-shaped constants but is missing
#: here (PHASE8_PLAN.md section 7.4) -- a new module cannot silently escape.
AUDITED_MODULES = [memory_repository, entity_repository, graph_repository]


def _cypher_constants(module: object) -> dict[str, str]:
    """Every module-level upper-case string constant that looks like Cypher."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper()
        and isinstance(value, str)
        and READ_WRITE_KEYWORDS.search(value)
    }


@pytest.mark.parametrize("module", AUDITED_MODULES)
def test_every_statement_filters_on_user_id(module: object) -> None:
    offenders = [
        name
        for name, query in _cypher_constants(module).items()
        if name not in FRAGMENT_NAMES and "$user_id" not in query
    ]
    assert offenders == [], (
        f"These Cypher statements do not filter on $user_id, which would let one "
        f"user read or write another user's memory: {offenders}"
    )


@pytest.mark.parametrize("module", AUDITED_MODULES)
def test_every_statement_is_registered_for_audit(module: object) -> None:
    """A new query must be added to USER_SCOPED_STATEMENTS, not silently escape review."""
    declared = set(module.USER_SCOPED_STATEMENTS)  # type: ignore[attr-defined]
    found = {
        name: query
        for name, query in _cypher_constants(module).items()
        if name not in FRAGMENT_NAMES
    }
    unregistered = sorted(
        name for name, query in found.items() if query not in declared
    )
    assert unregistered == [], (
        f"Cypher constants missing from USER_SCOPED_STATEMENTS: {unregistered}"
    )


def test_traversal_requires_every_hop_to_belong_to_the_user() -> None:
    """A multi-hop query must not be able to walk out of the user's own subgraph."""
    query = entity_repository.NEIGHBOURHOOD_NODES_TEMPLATE
    assert "all(r IN rels WHERE r.user_id = $user_id" in query
    assert "all(n IN nodes(path) WHERE n.user_id = $user_id)" in query


def test_only_the_hop_bound_is_interpolated() -> None:
    """The traversal template must have exactly one placeholder: the hop count."""
    template = entity_repository.NEIGHBOURHOOD_NODES_TEMPLATE
    placeholders = re.findall(r"\{(\w*)\}", template)
    assert placeholders == ["hops"], placeholders


def test_paths_template_cannot_walk_out_of_the_users_subgraph() -> None:
    """Phase 4's path-capturing sibling template needs the same isolation guard."""
    query = entity_repository.NEIGHBOURHOOD_PATHS_TEMPLATE
    assert "all(r IN rels WHERE r.user_id = $user_id" in query
    assert "all(n IN nodes(path) WHERE n.user_id = $user_id)" in query


def test_only_the_hop_bound_is_interpolated_in_paths_template() -> None:
    """Same audited-formatting contract as NEIGHBOURHOOD_NODES_TEMPLATE (this is
    a `.format()` template, not an f-string, so `{hops}` in the source is
    literal text until `.format(hops=validated_int)` runs at call time)."""
    template = entity_repository.NEIGHBOURHOOD_PATHS_TEMPLATE
    placeholders = re.findall(r"\{(\w*)\}", template)
    assert placeholders == ["hops"], placeholders


@pytest.mark.parametrize("module", AUDITED_MODULES)
def test_no_statement_uses_string_formatting_for_values(module: object) -> None:
    """Values must travel as parameters; only the audited hop bound is formatted in."""
    for name, query in _cypher_constants(module).items():
        if name in ("NEIGHBOURHOOD_NODES_TEMPLATE", "NEIGHBOURHOOD_PATHS_TEMPLATE"):
            continue
        assert "%s" not in query, name
        assert not re.search(r"\{\w+\}", query), name


class TestFilterComposition:
    def test_composed_search_query_retains_user_filter(self) -> None:
        where, _ = _build_filters({"query": "ocr", "statuses": ["active"]})
        composed = f"{SEARCH_MEMORIES_BASE}{where}{SEARCH_MEMORIES_RETURN}"
        assert "$user_id" in composed

    def test_caller_values_never_reach_the_query_text(self) -> None:
        """A hostile filter value must end up in params, not in the Cypher."""
        hostile = "x' OR 1=1 // DETACH DELETE n"
        where, params = _build_filters({"query": hostile})
        assert hostile not in where
        assert params["query"] == hostile
        assert "DETACH DELETE" not in where

    def test_unknown_filter_keys_are_ignored(self) -> None:
        """An unexpected key must not be able to inject a clause."""
        where, params = _build_filters({"; DETACH DELETE n //": "boom"})
        assert where == ""
        assert params == {}

    def test_empty_and_none_filters_produce_no_clause(self) -> None:
        where, params = _build_filters(
            {"query": None, "statuses": [], "memory_types": None}
        )
        assert where == ""
        assert params == {}

    def test_every_fragment_references_only_its_own_parameter(self) -> None:
        """Each fragment must reference $<key> (plus $user_id) and nothing else."""
        for key, fragment in FILTER_FRAGMENTS.items():
            referenced = set(re.findall(r"\$(\w+)", fragment))
            assert referenced <= {key, "user_id"}, (key, referenced)


def test_every_repository_module_with_cypher_is_audited() -> None:
    """Auto-discovery: walk app/repositories/ and find every module with
    Cypher-shaped constants; each must be in AUDITED_MODULES."""
    audited = {m.__name__ for m in AUDITED_MODULES}
    with_cypher = set()
    for info in pkgutil.iter_modules(repositories_package.__path__):
        module = importlib.import_module(f"{repositories_package.__name__}.{info.name}")
        if _cypher_constants(module):
            with_cypher.add(module.__name__)
    missing = sorted(with_cypher - audited)
    assert missing == [], f"Repository modules with Cypher not covered by this audit: {missing}"
    assert with_cypher, "discovery found no Cypher at all -- the walk itself is broken"


class TestGraphRepositoryStatements:
    """The Phase 8 projections: stricter checks than the shared ones above."""

    STATEMENTS = graph_repository.USER_SCOPED_STATEMENTS

    @pytest.mark.parametrize("query", STATEMENTS)
    def test_every_node_pattern_is_user_scoped(self, query: str) -> None:
        """Every labelled node pattern carries `user_id: $user_id`, so no
        traversal step can match another user's node."""
        for pattern in re.findall(r"\((\w*):(Entity|Memory|Source)\b([^)]*)\)", query):
            assert "user_id: $user_id" in pattern[2], pattern

    @pytest.mark.parametrize("query", STATEMENTS)
    def test_every_relates_to_edge_is_user_scoped(self, query: str) -> None:
        if ":RELATES_TO" in query:
            assert "r.user_id = $user_id" in query

    @pytest.mark.parametrize("query", STATEMENTS)
    def test_every_statement_is_bounded(self, query: str) -> None:
        """A LIMIT, or driven by a caller-bounded UNWIND list."""
        assert "LIMIT $limit" in query or query.lstrip().startswith("UNWIND $"), query

    @pytest.mark.parametrize("query", STATEMENTS)
    def test_every_statement_is_read_only(self, query: str) -> None:
        assert not re.search(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DETACH)\b", query), query

    def test_no_hop_bound_is_interpolated_at_all(self) -> None:
        """Traversal is per-hop BFS from Python; no variable-length pattern."""
        for query in self.STATEMENTS:
            assert "*1.." not in query and "*.." not in query

    def test_temporal_comparisons_are_instant_based(self) -> None:
        """Window comparisons normalise both sides to UTC (a named-zone and an
        offset-zone DateTime at the same instant do not compare equal in Neo4j)."""
        for query in self.STATEMENTS:
            for side in re.findall(r"(\S+)\s*(?:<=|>=|<|>)\s*(\S+)", query):
                joined = " ".join(side)
                if "valid_" in joined or "$at" in joined:
                    assert "timezone: '+00:00'" in query
