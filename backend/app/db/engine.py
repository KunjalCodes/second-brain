"""PostgreSQL connection layer.

The counterpart to `app.graph.connection`: the only module that owns the engine, so
the rest of the application sees sessions and application errors rather than driver
details.

Credentials come exclusively from `DATABASE_URL`. As with Neo4j there is no localhost
default, because a silent fallback would make a misconfigured production deploy look
healthy.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.errors import (
    AppError,
    ConfigurationError,
    ConflictError,
    DatabaseError,
    DatabaseUnavailableError,
)
from app.db.models import EMBEDDING_DIMENSION

logger = logging.getLogger(__name__)


def map_database_error(exc: Exception) -> Exception:
    """Translate a SQLAlchemy exception into the application's error taxonomy.

    ARCHITECTURE.md section 21 requires database failures to be distinguishable from
    validation and authorization failures. SQL text is logged, never returned.
    """
    if isinstance(exc, IntegrityError):
        # Unique/foreign-key violations are expected control flow in the extraction
        # pipeline (idempotency), so they must be distinguishable from real faults.
        return ConflictError("The write violates a database constraint")
    if isinstance(exc, OperationalError):
        logger.error("PostgreSQL unavailable: %s", exc)
        return DatabaseUnavailableError(
            "The database is currently unreachable. Please retry shortly."
        )
    if isinstance(exc, DBAPIError | SQLAlchemyError):
        logger.exception("Database query failed")
        return DatabaseError("The database query failed")
    return exc


class Database:
    """Owns the async engine and session factory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        if self._engine is not None:
            return
        s = self._settings
        if not s.database_configured:
            raise ConfigurationError(
                "PostgreSQL is not configured. Set DATABASE_URL (see .env.example)."
            )
        if s.embedding_dimension != EMBEDDING_DIMENSION:
            # The pgvector column's width is a SCHEMA property, fixed by the
            # migration that created it (ADR 0009, PHASE3_PLAN.md section 12) --
            # not something an environment variable can change at runtime. A
            # mismatch here means the deployed schema and the configured
            # embedding model disagree about vector width, which would otherwise
            # surface as an opaque pgvector dimension error on the first write or
            # query. Fail loudly at boot instead, matching the production guard
            # already applied to llm_provider.
            raise ConfigurationError(
                f"EMBEDDING_DIMENSION={s.embedding_dimension} does not match the "
                f"pgvector column's declared width ({EMBEDDING_DIMENSION}). "
                "Changing the embedding dimension requires a new migration, not "
                "just a changed environment variable."
            )
        self._engine = create_async_engine(
            s.database_url,
            pool_size=s.database_pool_size,
            max_overflow=s.database_max_overflow,
            pool_pre_ping=True,  # managed providers drop idle connections
            echo=s.database_echo,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )
        logger.info("PostgreSQL engine created for %s", _safe_dsn(s.database_url))

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("PostgreSQL engine disposed")

    @property
    def is_connected(self) -> bool:
        return self._engine is not None

    async def verify(self) -> None:
        """Actively check that the database answers."""
        if self._engine is None:
            raise DatabaseUnavailableError("The database engine has not been initialised")
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            raise map_database_error(exc) from exc

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session with commit-on-success, rollback-on-error semantics."""
        if self._sessionmaker is None:
            raise DatabaseUnavailableError(
                "The database engine has not been initialised. This usually means the "
                "application failed to start up correctly."
            )
        session = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except AppError:
            await session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            raise map_database_error(exc) from exc
        finally:
            await session.close()


def _safe_dsn(url: str) -> str:
    """Strip credentials before logging a connection string."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database()
    return _database


def reset_database() -> None:
    """Drop the process-wide database object. Used by tests."""
    global _database
    _database = None
