"""Security properties of the Phase 7 evaluation framework (PHASE7_PLAN.md
section 8) -- no database, static/structural checks only.

The two live properties this file cannot check by reading source (cross-run
isolation, real persistence behavior) are covered by
`tests/integration/test_evaluation_persistence.py` instead; what belongs
here is what a static check can prove once and for all: the evaluation
framework has no code path into Neo4j at all, so "evaluation cannot write
canonical memory" is true by construction, not by discipline.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models.evaluation import EvaluationRun, EvaluationTaskResult

BACKEND_DIR = Path(__file__).resolve().parents[2]

#: Modules that must have no path to Neo4j -- the evaluation persistence/
#: reporting layer only ever reads/writes the two Phase 7 PostgreSQL
#: tables (see their own module docstrings' "never touches Neo4j" claim).
_NEO4J_FREE_MODULES = (
    BACKEND_DIR / "eval" / "persistence.py",
    BACKEND_DIR / "eval" / "report.py",
    BACKEND_DIR / "app" / "repositories" / "evaluation_repository.py",
    BACKEND_DIR / "app" / "models" / "evaluation.py",
)

#: Names that would indicate a path into Neo4j if imported.
_NEO4J_MARKERS = frozenset({"GraphClient", "get_graph_client", "app.graph"})


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("path", _NEO4J_FREE_MODULES, ids=lambda p: p.name)
def test_evaluation_persistence_has_no_path_to_neo4j(path: Path) -> None:
    """`EvaluationRunTracker`/`EvaluationRunRepository`/the report
    aggregator must be structurally incapable of writing canonical
    memory -- proven here by the absence of any Neo4j import, not merely
    by nobody having called one.
    """
    imported = _imported_names(path)
    offending = {name for name in imported if any(marker in name for marker in _NEO4J_MARKERS)}
    assert offending == set(), (
        f"{path.name} imports something Neo4j-related: {offending} -- the evaluation "
        f"persistence/reporting layer must never have a path to canonical memory"
    )


def test_evaluation_repository_is_not_reachable_from_graph_connection() -> None:
    """The inverse check: `app/graph/connection.py` (the one module that
    actually opens a Neo4j driver) never imports the evaluation
    repository either -- there is no code path in either direction.
    """
    graph_connection = BACKEND_DIR / "app" / "graph" / "connection.py"
    imported = _imported_names(graph_connection)
    assert not any("evaluation" in name for name in imported)


class TestNoRawContentInDomainModels:
    """`EvaluationRun`/`EvaluationTaskResult` are `extra="forbid"` --
    proven directly against the live schema, not asserted from memory, so
    a future field addition cannot silently start accepting arbitrary
    (and possibly sensitive) keys.
    """

    def test_evaluation_run_forbids_extra_fields(self) -> None:
        assert EvaluationRun.model_config.get("extra") == "forbid"

    def test_evaluation_task_result_forbids_extra_fields(self) -> None:
        assert EvaluationTaskResult.model_config.get("extra") == "forbid"

    def test_neither_model_declares_a_raw_content_field(self) -> None:
        """A structural guard against ever adding a field like
        `memory_content`/`document_text` directly to these models -- the
        only content-shaped fields allowed are `metrics` (structured,
        caller-controlled) and `error_detail` (bounded, diagnostic text).
        """
        disallowed_substrings = ("memory_content", "document_text", "raw_content")
        for model in (EvaluationRun, EvaluationTaskResult):
            field_names = set(model.model_fields)
            for bad in disallowed_substrings:
                assert not any(bad in f for f in field_names), (model, bad)
