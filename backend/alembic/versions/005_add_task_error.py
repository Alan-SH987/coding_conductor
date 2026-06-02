"""Add error column to Task table.

Revision ID: 005
Revises: 004
Create Date: 2026-06-02

Idempotent (same shape as 003/004): init_db runs SQLModel.create_all first, so
on a fresh DB the column already exists; we only add what's missing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if "error" not in _existing_columns("task"):
        with op.batch_alter_table("task", schema=None) as batch_op:
            batch_op.add_column(sa.Column("error", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("error")
