"""Static audit that no repository bypasses `UserScopedRepository`.

The SQL counterpart to `tests/unit/test_cypher_safety.py`. Isolation for
PostgreSQL-backed tables (docs/adr/0006) is enforced by one rule: every query is
built through `UserScopedRepository._select` / `_update` / `_delete`
(`app/repositories/sql_base.py`), never through a bare `select`/`update`/`delete`
call, so the `user_id` filter cannot be forgotten at a call site.

This is a **weaker** guarantee than the Cypher audit: SQLAlchemy statements are
Python objects built programmatically, not inspectable query strings, so an AST
check can only catch how a statement is *constructed*, not prove the final
statement actually carries the filter. That's why it exists alongside, not instead
of, per-repository isolation tests (see `tests/unit/test_conversation_service.py`
and the isolation cases in the integration suite) -- the AST check catches an
accidental bypass early and cheaply; the isolation tests catch what it cannot.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: These names, imported directly from sqlalchemy, construct a statement with no
#: isolation filter attached. Only sql_base.py may import them.
FORBIDDEN_NAMES = frozenset({"select", "update", "delete"})

#: Files allowed to import the forbidden names directly:
#:
#: * sql_base.py       defines the helpers everything else must go through instead.
#: * extraction_queue.py  is the one deliberately cross-user module: claiming,
#:   leasing and completing jobs is, by design, not scoped to a single user (a
#:   worker tick processes every user's backlog in one pass). See its module
#:   docstring. Every method that CAN be scoped to one user lives in
#:   extraction_repository.py instead, which is not exempted.
#: * user_repository.py  `users` has no `user_id` column to scope against (a row's
#:   own `id` *is* the isolation key), and `list_ids` (Phase 3) is a deliberate,
#:   bounded cross-user enumeration for the embedding backfill sweep -- the same
#:   "must walk every user by design" shape as extraction_queue.py. See its module
#:   docstring.
#: * ingestion_queue.py  is Phase 6's document-ingestion analogue of
#:   extraction_queue.py, exempt for the identical reason: claiming, leasing
#:   and completing ingestion jobs is deliberately cross-user (a worker tick
#:   processes every user's backlog in one pass). See its module docstring.
#:   Every method that CAN be scoped to one user lives in
#:   document_repository.py/ingestion_repository.py instead, which are not
#:   exempted.
#: * evaluation_repository.py  is Phase 7's evaluation-run persistence --
#:   exempt for a DIFFERENT reason than the three above: `evaluation_runs`/
#:   `evaluation_task_results` have no `user_id` column at all (they are
#:   operational/developer metadata about system performance, not any
#:   user's data -- see `app.models.evaluation`'s module docstring), so
#:   there is no isolation filter to forget in the first place. See its
#:   module docstring.
#: * auth_repository.py  is Phase 9's account/session persistence. Rows are found
#:   BY CREDENTIAL (unique normalised email, or SHA-256 of a session token)
#:   before any user is known, so there is no caller-supplied user to scope by;
#:   every write is keyed by the already-resolved user id or session id, and it
#:   never touches memory data (ADR 0020). See its module docstring.
#: * rate_limit_repository.py  is Phase 9's abuse-control counter table: no
#:   user-owned data, only `<class>:<identity>` keys and counts, and one limiter
#:   must count across all callers (ADR 0023). See its module docstring.
#: * project_repository.py  is Phase 16's project and project-token persistence. A
#:   token is found BY CREDENTIAL (its SHA-256) before any user is known; every
#:   other read is keyed by owner_user_id AND project id together. It never
#:   reads memory data (docs/PHASE16_ARCHITECTURE.md section 9).
#: * project_event_queue.py  is Phase 16's sweeper enumeration of agent events that
#:   still need projecting, across every memory space by design (like
#:   extraction_queue.py). It returns only (space id, event id) pairs.
#: * ops_repository.py  is Phase 11's operational gauges for /internal/metrics:
#:   aggregate COUNTS and ages only across all users (queue depth, stuck
#:   documents, active sessions) -- it never returns a row, id or content.
EXEMPT_FILES = frozenset(
    {
        "sql_base.py",
        "extraction_queue.py",
        "user_repository.py",
        "ingestion_queue.py",
        "evaluation_repository.py",
        "auth_repository.py",
        "rate_limit_repository.py",
        "project_repository.py",
        "project_event_queue.py",
        "ops_repository.py",
        "__init__.py",
    }
)

REPO_DIR = Path(__file__).resolve().parents[2] / "app" / "repositories"


def _sqlalchemy_module_aliases(tree: ast.Module) -> set[str]:
    """Names bound to `import sqlalchemy` (or `import sqlalchemy as sa`)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[str] = []

    # Case 1: `from sqlalchemy import select` (with or without an alias).
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    violations.append(
                        f"{path.name}:{node.lineno}: `from sqlalchemy import "
                        f"{alias.name}` bypasses UserScopedRepository -- use "
                        f"self._{alias.name}(...) instead"
                    )

    # Case 2: `sqlalchemy.select(...)` / `sa.select(...)` via a module import.
    module_aliases = _sqlalchemy_module_aliases(tree)
    if module_aliases:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in FORBIDDEN_NAMES
                and isinstance(node.value, ast.Name)
                and node.value.id in module_aliases
            ):
                violations.append(
                    f"{path.name}:{node.lineno}: `{node.value.id}.{node.attr}(...)` "
                    f"bypasses UserScopedRepository -- use self._{node.attr}(...) "
                    f"instead"
                )

    return violations


def _repository_files() -> list[Path]:
    return sorted(p for p in REPO_DIR.glob("*.py") if p.name not in EXEMPT_FILES)


@pytest.mark.parametrize("path", _repository_files(), ids=lambda p: p.name)
def test_repository_does_not_bypass_user_scoped_helpers(path: Path) -> None:
    offenders = _violations(path)
    assert offenders == [], "\n".join(offenders)


def test_exemptions_are_exactly_the_documented_set() -> None:
    """Confirms the exemption is exactly these files, not an accidentally-widened set.

    Widening this set is a real decision (it removes a repository from the static
    audit) and must be deliberate -- if this test needs updating, the module
    docstring above should explain why the new file is exempt.
    """
    assert EXEMPT_FILES - {"__init__.py"} == {
        "sql_base.py",
        "extraction_queue.py",
        "user_repository.py",
        "ingestion_queue.py",
        "evaluation_repository.py",
        "auth_repository.py",
        "rate_limit_repository.py",
        "project_repository.py",
        "project_event_queue.py",
        "ops_repository.py",
    }


def test_user_scoped_repository_filters_every_statement_kind() -> None:
    """`_select`, `_update` and `_delete` must each attach the user_id predicate.

    Read as a guard against a future edit to sql_base.py that adds a new helper
    method without the filter, or removes the filter from an existing one.
    """
    import inspect

    from app.repositories.sql_base import UserScopedRepository

    for name in ("_select", "_update", "_delete"):
        source = inspect.getsource(getattr(UserScopedRepository, name))
        assert "self._user_id" in source, name
        assert ".where(" in source, name
