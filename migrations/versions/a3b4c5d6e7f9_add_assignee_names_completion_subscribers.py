"""Add assignee_names and completion_subscribers columns

Revision ID: a3b4c5d6e7f9
Revises: 2c12be573b1b
Create Date: 2026-02-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f9'
down_revision: Union[str, Sequence[str], None] = '2c12be573b1b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add JSONB columns for multi-assignee and completion subscriber tracking."""
    op.add_column('tasks', sa.Column('assignee_names', JSONB(), nullable=True))
    op.add_column('tasks', sa.Column('completion_subscribers', JSONB(), nullable=True))


def downgrade() -> None:
    """Remove assignee_names and completion_subscribers columns."""
    op.drop_column('tasks', 'completion_subscribers')
    op.drop_column('tasks', 'assignee_names')
