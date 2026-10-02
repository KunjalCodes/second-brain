"""phase16 projects

Additive only (docs/PHASE16_ARCHITECTURE.md sections 3, 4, 9): two new tables,
`projects` (a project is a memory space with its own `users` row) and
`project_tokens` (scoped, hashed credentials bound to one project). No existing
table is altered and no data is rewritten.

Revision ID: 3397424d24ed
Revises: 320f6d898a92
Create Date: 2026-10-01 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3397424d24ed'
down_revision: str | Sequence[str] | None = '320f6d898a92'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('projects',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('owner_user_id', sa.String(length=128), nullable=False),
    sa.Column('space_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('identity_key', sa.String(length=64), nullable=False),
    sa.Column('identity_kind', sa.String(length=16), nullable=False),
    sa.Column('default_branch', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("identity_kind IN ('remote_url','root_commits')", name='projects_identity_kind_valid'),
    sa.CheckConstraint('char_length(identity_key) = 64', name='projects_identity_key_len'),
    sa.CheckConstraint("status IN ('active','archived')", name='projects_status_valid'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['space_id'], ['users.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_user_id', 'identity_key', name='projects_owner_identity'),
    sa.UniqueConstraint('space_id', name='projects_space_unique')
    )
    op.create_index('projects_owner', 'projects', ['owner_user_id', 'created_at'], unique=False)
    op.create_table('project_tokens',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('project_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('token_sha256', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=80), nullable=False),
    sa.Column('scopes', postgresql.ARRAY(sa.String(length=16)), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('cardinality(scopes) >= 1', name='project_tokens_scopes_nonempty'),
    sa.CheckConstraint('char_length(token_sha256) = 64', name='project_tokens_hash_len'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_sha256')
    )
    op.create_index('project_tokens_project', 'project_tokens', ['project_id', 'revoked_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('project_tokens_project', table_name='project_tokens')
    op.drop_table('project_tokens')
    op.drop_index('projects_owner', table_name='projects')
    op.drop_table('projects')
