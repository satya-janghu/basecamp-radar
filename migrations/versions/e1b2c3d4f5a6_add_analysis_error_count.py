"""Add analysis_error_count column for retry backoff

Revision ID: e1b2c3d4f5a6
Revises: d9a4b5c6e7f8
Create Date: 2026-02-08 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1b2c3d4f5a6'
down_revision: Union[str, Sequence[str], None] = 'd9a4b5c6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add analysis_error_count column to tasks."""
    op.add_column('tasks', sa.Column(
        'analysis_error_count', sa.Integer(), nullable=False, server_default='0',
    ))


def downgrade() -> None:
    """Remove analysis_error_count column."""
    op.drop_column('tasks', 'analysis_error_count')
