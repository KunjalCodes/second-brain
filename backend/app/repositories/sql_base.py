"""Base class enforcing user isolation for every SQL repository.

This is the PostgreSQL counterpart to the Cypher `$user_id` discipline in
`app/repositories/memory_repository.py`. Every SQL repository in this package must
extend `UserScopedRepository` and build statements through `_select`, `_update` and
`_delete`, which apply the `user_id` filter automatically -- there is no other
sanctioned way to query a user-owned table in this codebase.

`tests/unit/test_sql_safety.py` statically audits this: it walks the AST of every
module under `app/repositories/` and fails if a bare `select(`, `update(` or
`delete(` call appears outside this module. That is a weaker guarantee than the
Cypher audit (SQLAlchemy statements are Python objects, not inspectable query
strings, so the AST check can only catch the *construction* site, not prove the
final statement carries the filter) -- which is exactly why it is backed by
per-repository isolation tests rather than relied on alone.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Delete, Select, Update, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

ModelT = TypeVar("ModelT", bound=DeclarativeBase)


class UserScopedRepository:
    """Base class for repositories that read or write user-owned PostgreSQL rows.

    Every subclass method must build its statement through `_select`/`_update`/
    `_delete` rather than importing `select`/`update`/`delete` directly, so that the
    `user_id` filter cannot be forgotten at a call site.
    """

    def __init__(self, session: AsyncSession, *, user_id: str) -> None:
        self._session = session
        self._user_id = user_id

    def _select(self, model: type[ModelT]) -> Select:
        """`SELECT * FROM <model> WHERE <model>.user_id = :user_id`."""
        return select(model).where(model.user_id == self._user_id)  # type: ignore[attr-defined]

    def _select_columns(self, model: type[ModelT], *columns: ColumnElement[Any]) -> Select:
        """`SELECT <columns> FROM <model> WHERE <model>.user_id = :user_id`.

        For queries that need specific columns or computed expressions (e.g. a
        pgvector distance expression) rather than whole rows -- same isolation
        guarantee as `_select`, just not bound to returning full ORM objects.
        """
        return select(*columns).select_from(model).where(model.user_id == self._user_id)  # type: ignore[attr-defined]

    def _update(self, model: type[ModelT]) -> Update:
        """`UPDATE <model> ... WHERE <model>.user_id = :user_id`."""
        return update(model).where(model.user_id == self._user_id)  # type: ignore[attr-defined]

    def _delete(self, model: type[ModelT]) -> Delete:
        """`DELETE FROM <model> WHERE <model>.user_id = :user_id`.

        Unused in Phase 2 -- every table here is append-only, matching the Neo4j
        memory model -- but defined for completeness and so a future genuine delete
        (e.g. GDPR erasure) inherits the isolation guarantee rather than bypassing
        this class entirely.
        """
        return delete(model).where(model.user_id == self._user_id)  # type: ignore[attr-defined]
