"""Add is_pinned, is_archived, deleted_at columns to Project and Task tables.

Revision ID: 001
Revises:
Create Date: 2025-06-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use batch mode for SQLite compatibility
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("deleted_at")

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("is_archived")
        batch_op.drop_column("is_pinned")
