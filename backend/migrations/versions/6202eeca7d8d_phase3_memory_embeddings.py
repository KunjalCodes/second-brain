"""phase3 memory embeddings

Revision ID: 6202eeca7d8d
Revises: 7fcb56a35aa9
Create Date: 2026-09-18 14:13:49.761623

ADR 0009: pgvector holds a disposable, regenerable vector representation of a
canonical Neo4j Memory. `memory_id` is an opaque pointer into Neo4j (like
`extraction_candidates.memory_id`), never a foreign key -- Neo4j is not a table
this database can reference. A memory's content, status and validity window are
never copied here.

Index choice (PHASE3_PLAN.md section 15): HNSW, not IVFFlat. IVFFlat needs a
representative sample to train cluster centroids before it is useful, which is
actively counterproductive on a table that starts small/empty; HNSW builds
incrementally with no training step. Cosine distance (`vector_cosine_ops`) is the
standard choice for general-purpose text embeddings. Default `m`/`ef_construction`
are used deliberately -- tuning either is deferred to real benchmarking
(PHASE3_PLAN.md section 20), not guessed at migration-authoring time.
"""
from typing import Sequence, Union

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6202eeca7d8d'
down_revision: Union[str, Sequence[str], None] = '7fcb56a35aa9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        'memory_embeddings',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('user_id', sa.String(length=128), nullable=False),
        sa.Column('memory_id', sa.String(length=64), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.Vector(768), nullable=False),
        sa.Column('embedding_provider', sa.String(length=50), nullable=False),
        sa.Column('embedding_model', sa.String(length=200), nullable=False),
        sa.Column('embedding_dimension', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'memory_id', 'embedding_provider', 'embedding_model',
            name='memory_embeddings_one_per_model',
        ),
    )
    op.create_index('memory_embeddings_memory', 'memory_embeddings', ['memory_id'], unique=False)
    op.create_index('memory_embeddings_user', 'memory_embeddings', ['user_id'], unique=False)

    # HNSW cosine-distance index -- not expressible via SQLAlchemy's ORM Index
    # construct (it does not model pgvector's access-method syntax), so it is
    # created here directly, in the one place that is the authoritative record of
    # its exact configuration.
    op.execute(
        "CREATE INDEX memory_embeddings_hnsw_cosine ON memory_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS memory_embeddings_hnsw_cosine")
    op.drop_index('memory_embeddings_user', table_name='memory_embeddings')
    op.drop_index('memory_embeddings_memory', table_name='memory_embeddings')
    op.drop_table('memory_embeddings')
    # The `vector` extension is intentionally left installed on downgrade: other
    # objects may depend on it, and dropping a shared extension from a single
    # migration's downgrade is unsafe.
