"""Star feature + remove brain system

Revision ID: c7f2a3b9d1e8
Revises: b8e3f1a2c4d5
Create Date: 2026-02-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7f2a3b9d1e8'
down_revision: Union[str, Sequence[str], None] = 'b8e3f1a2c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_starred to tasks, remove brain_context_path from accounts, drop brain_state table."""
    # Star feature
    op.add_column('tasks', sa.Column('is_starred', sa.Boolean(), nullable=False, server_default=sa.text('false')))

    # Remove brain system
    op.drop_column('accounts', 'brain_context_path')
    op.drop_table('brain_state')


def downgrade() -> None:
    """Reverse star feature + brain removal."""
    # Restore brain_state table
    op.create_table('brain_state',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('file_path', sa.String(), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=True),
        sa.Column('last_modified', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    # Restore brain_context_path
    op.add_column('accounts', sa.Column('brain_context_path', sa.String(), nullable=True))

    # Remove star feature
    op.drop_column('tasks', 'is_starred')
