"""phase16 project history

Additive only (docs/PHASE16_ARCHITECTURE.md sections 5-8): `agent_sessions`,
`agent_events` (the auditable raw event log, with an idempotency key and a
projection status) and `git_commits` (structured commit metadata, no diffs). All
three are scoped by `user_id` = the project's memory-space id. No existing table is
altered.

Revision ID: b2f392141c1b
Revises: 3397424d24ed
Create Date: 2026-10-01 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2f392141c1b'
down_revision: str | Sequence[str] | None = '3397424d24ed'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('agent_sessions',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('project_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_key', sa.String(length=64), nullable=False),
    sa.Column('agent_name', sa.String(length=80), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=16), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_count', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint("outcome IS NULL OR outcome IN ('completed','blocked','abandoned')", name='agent_sessions_outcome_valid'),
    sa.CheckConstraint("status IN ('active','ended')", name='agent_sessions_status_valid'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'session_key', name='agent_sessions_key_unique')
    )
    op.create_index('agent_sessions_recent', 'agent_sessions', ['user_id', 'project_id', 'last_event_at'], unique=False)
    op.create_table('agent_events',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('project_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('session_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('recorded_seq', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('event_type', sa.String(length=24), nullable=False),
    sa.Column('event_key', sa.String(length=128), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=80), nullable=True),
    sa.Column('projection_status', sa.String(length=12), server_default='pending', nullable=False),
    sa.Column('memory_id', sa.String(length=64), nullable=True),
    sa.Column('projection_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('projection_error', sa.String(length=300), nullable=True),
    sa.Column('projected_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("event_type IN ('session_started','session_ended','user_request','decision','file_inspected','file_modified','bug_discovered','error','fix','test_run','commit','task_completed','task_blocked','memory_proposal')", name='agent_events_type_valid'),
    sa.CheckConstraint('char_length(payload_sha256) = 64', name='agent_events_hash_len'),
    sa.CheckConstraint("projection_status IN ('pending','projected','skipped','failed')", name='agent_events_projection_valid'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['agent_sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'event_key', name='agent_events_idempotency')
    )
    op.create_index('agent_events_project_recent', 'agent_events', ['user_id', 'project_id', 'recorded_seq'], unique=False)
    op.create_index('agent_events_session_order', 'agent_events', ['user_id', 'session_id', 'recorded_seq'], unique=False)
    op.create_index('agent_events_unprojected', 'agent_events', ['projection_status', 'recorded_seq'], unique=False, postgresql_where=sa.text("projection_status IN ('pending','failed')"))
    op.create_table('git_commits',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('project_id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('sha', sa.String(length=64), nullable=False),
    sa.Column('parents', postgresql.ARRAY(sa.String(length=64)), nullable=False),
    sa.Column('author_name', sa.String(length=200), nullable=True),
    sa.Column('author_email_sha256', sa.String(length=64), nullable=True),
    sa.Column('authored_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('committed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('subject', sa.String(length=500), nullable=False),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('body_truncated', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('branches', postgresql.ARRAY(sa.String(length=255)), nullable=False),
    sa.Column('files', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('files_changed_count', sa.Integer(), nullable=False),
    sa.Column('files_truncated', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('additions', sa.Integer(), nullable=True),
    sa.Column('deletions', sa.Integer(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('projection_status', sa.String(length=12), server_default='pending', nullable=False),
    sa.Column('memory_id', sa.String(length=64), nullable=True),
    sa.Column('projection_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('projection_error', sa.String(length=300), nullable=True),
    sa.CheckConstraint("projection_status IN ('pending','projected','skipped','failed')", name='git_commits_projection_valid'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'sha', name='git_commits_sha_unique')
    )
    op.create_index('git_commits_recent', 'git_commits', ['user_id', 'project_id', 'committed_at'], unique=False)
    op.create_index('git_commits_unprojected', 'git_commits', ['projection_status', 'committed_at'], unique=False, postgresql_where=sa.text("projection_status IN ('pending','failed')"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('git_commits_unprojected', table_name='git_commits', postgresql_where=sa.text("projection_status IN ('pending','failed')"))
    op.drop_index('git_commits_recent', table_name='git_commits')
    op.drop_table('git_commits')
    op.drop_index('agent_events_unprojected', table_name='agent_events', postgresql_where=sa.text("projection_status IN ('pending','failed')"))
    op.drop_index('agent_events_session_order', table_name='agent_events')
    op.drop_index('agent_events_project_recent', table_name='agent_events')
    op.drop_table('agent_events')
    op.drop_index('agent_sessions_recent', table_name='agent_sessions')
    op.drop_table('agent_sessions')
