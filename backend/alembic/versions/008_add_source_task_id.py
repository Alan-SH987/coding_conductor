"""Add source_task_id column to Task table for provenance tracking.

Revision ID: 008
Revises: 007
Create Date: 2026-06-02

Idempotent: init_db runs SQLModel.create_all first, so on a fresh DB the column
already exists; we only add what's missing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if "source_task_id" not in _existing_columns("task"):
        with op.batch_alter_table("task", schema=None) as batch_op:
            batch_op.add_column(sa.Column("source_task_id", sa.Integer(), nullable=True))
            # Create foreign key constraint to task.id
            batch_op.create_foreign_key(
                "fk_task_source_task_id",
                "task",
                ["source_task_id"],
                ["id"],
            )


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_constraint("fk_task_source_task_id", type_="foreignkey")
        batch_op.drop_column("source_task_id")
