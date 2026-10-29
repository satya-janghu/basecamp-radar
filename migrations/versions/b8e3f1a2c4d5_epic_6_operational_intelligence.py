"""Epic 6: operational intelligence

Revision ID: b8e3f1a2c4d5
Revises: 4649a50e1df6
Create Date: 2026-02-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8e3f1a2c4d5'
down_revision: Union[str, Sequence[str], None] = '4649a50e1df6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add operational intelligence columns and tables."""
    # New columns on tasks
    op.add_column('tasks', sa.Column('last_analyzed_comments_hash', sa.String(), nullable=True))
    op.add_column('tasks', sa.Column('previous_score', sa.Integer(), nullable=True))
    op.add_column('tasks', sa.Column('previous_tier', sa.String(), nullable=True))

    # Drop deprecated columns from tasks
    op.drop_column('tasks', 'snoozed_until')
    op.drop_column('tasks', 'is_dismissed')

    # New poll_runs table
    op.create_table('poll_runs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Reverse Epic 6 changes."""
    op.drop_table('poll_runs')

    op.add_column('tasks', sa.Column('is_dismissed', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('tasks', sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True))

    op.drop_column('tasks', 'previous_tier')
    op.drop_column('tasks', 'previous_score')
    op.drop_column('tasks', 'last_analyzed_comments_hash')
