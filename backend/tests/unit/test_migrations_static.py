"""Phase 11 (M7): migration hygiene checked without a database."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _script() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return ScriptDirectory.from_config(config)


def _env_namespace() -> dict:
    """Execute only the RAW_SQL_MANAGED_OBJECTS / include_object definitions
    from env.py (the module itself runs migrations on import)."""
    tree = ast.parse((MIGRATIONS / "env.py").read_text())
    keep = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "RAW_SQL_MANAGED_OBJECTS"
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "include_object")
    ]
    namespace: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "env.py", "exec"), namespace)  # noqa: S102
    return namespace


def test_exactly_one_head() -> None:
    assert len(_script().get_heads()) == 1


def test_revision_chain_is_linear() -> None:
    for revision in _script().walk_revisions():
        down = revision.down_revision
        assert down is None or isinstance(down, str), f"{revision.revision} is a merge point"


def test_every_raw_sql_index_is_protected_from_autogenerate() -> None:
    """Any index a migration creates with raw SQL must be in
    RAW_SQL_MANAGED_OBJECTS, or autogenerate will propose dropping it."""
    namespace = _env_namespace()
    managed = namespace["RAW_SQL_MANAGED_OBJECTS"]
    include_object = namespace["include_object"]
    raw = set()
    for path in (MIGRATIONS / "versions").glob("*.py"):
        raw |= set(
            re.findall(
                r"CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?(\w+)",
                path.read_text(),
            )
        )
    assert "memory_embeddings_hnsw_cosine" in raw
    for name in raw:
        assert ("index", name) in managed, f"raw-SQL index {name} is not protected"
        assert include_object(None, name, "index", True, None) is False
    assert include_object(None, "users", "table", True, None) is True
