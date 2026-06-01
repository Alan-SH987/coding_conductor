"""Add is_pinned, is_archived, deleted_at columns to Project and Task tables.

Revision ID: 001
Revises:
Create Date: 2025-06-01

Idempotent: init_db runs SQLModel.create_all (which builds the full current
schema on a fresh DB) before this migration, so on a fresh DB the columns
already exist. We only add what's missing, so the same revision works whether
the DB pre-dates these columns or was just created by create_all.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    project_cols = _existing_columns("project")
    to_add = []
    if "is_pinned" not in project_cols:
        to_add.append(sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default="0"))
    if "is_archived" not in project_cols:
        to_add.append(sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="0"))
    if "deleted_at" not in project_cols:
        to_add.append(sa.Column("deleted_at", sa.DateTime(), nullable=True))
    if to_add:
        with op.batch_alter_table("project", schema=None) as batch_op:
            for col in to_add:
                batch_op.add_column(col)

    if "deleted_at" not in _existing_columns("task"):
        with op.batch_alter_table("task", schema=None) as batch_op:
            batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_column("deleted_at")

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("is_archived")
        batch_op.drop_column("is_pinned")
