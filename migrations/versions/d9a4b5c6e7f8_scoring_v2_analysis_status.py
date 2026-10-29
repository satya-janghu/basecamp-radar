"""Scoring v2: analysis_status column + composite index

Revision ID: d9a4b5c6e7f8
Revises: c7f2a3b9d1e8
Create Date: 2026-02-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd9a4b5c6e7f8'
down_revision: Union[str, Sequence[str], None] = 'c7f2a3b9d1e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add analysis_status column and composite index for dashboard queries."""
    # Analysis status: pending | partial | complete | stale
    op.add_column('tasks', sa.Column(
        'analysis_status', sa.String(), nullable=True, server_default='pending',
    ))

    # Set existing tasks: those with ai_signals -> complete, others -> pending
    op.execute(
        "UPDATE tasks SET analysis_status = 'complete' WHERE ai_signals IS NOT NULL"
    )
    op.execute(
        "UPDATE tasks SET analysis_status = 'pending' WHERE ai_signals IS NULL AND analysis_status IS NULL"
    )

    # Composite index for dashboard queries (account_id + status + priority_tier)
    op.create_index(
        'ix_tasks_account_status_tier',
        'tasks',
        ['account_id', 'status', 'priority_tier'],
    )


def downgrade() -> None:
    """Remove analysis_status column and composite index."""
    op.drop_index('ix_tasks_account_status_tier', table_name='tasks')
    op.drop_column('tasks', 'analysis_status')
