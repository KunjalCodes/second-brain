import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# So `app` resolves whether alembic is invoked from backend/ or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import (  # noqa: E402, F401
    Conversation,
    EntityMergeCandidate,
    ExtractionCandidate,
    ExtractionJob,
    Message,
    User,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL from the application's own settings (env vars / .env) is the single
# source of truth for where migrations run -- never a URL hardcoded in alembic.ini.
# This is what lets the same migration set target a local database, Supabase or Neon
# with nothing but an environment variable changing.
_settings = get_settings()
if _settings.database_url:
    config.set_main_option("sqlalchemy.url", _settings.database_url)

target_metadata = Base.metadata

#: Database objects created with RAW SQL in a migration and deliberately NOT
#: declared on the ORM models (Phase 11, M7). Autogenerate compares the models
#: to the live database, so without this list it proposes DROPPING these on
#: every run -- `memory_embeddings_hnsw_cosine` (the pgvector HNSW index,
#: created in 6202eeca7d8d) was hand-removed from five generated migrations
#: before this guard existed. Add an object here in the same migration that
#: creates it with `op.execute`.
RAW_SQL_MANAGED_OBJECTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("index", "memory_embeddings_hnsw_cosine"),
    }
)


def include_object(object_, name, type_, reflected, compare_to):  # noqa: ANN001, ANN201
    """Keep raw-SQL-managed objects out of autogenerate's comparison, so it
    can never propose removing them."""
    return (type_, name) not in RAW_SQL_MANAGED_OBJECTS

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
