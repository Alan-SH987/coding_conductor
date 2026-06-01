"""Add TaskSuggestion table for tracking next-step suggestions.

Revision ID: 002
Revises: 001
Create Date: 2026-06-01

Idempotent: init_db runs SQLModel.create_all (which builds the full current
schema on a fresh DB) before this migration, so on a fresh DB the table
already exists. We only create the table if it's missing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return table in insp.get_table_names()


def upgrade() -> None:
    if not _table_exists("tasksuggestion"):
        op.create_table(
            "tasksuggestion",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("suggested_task_ids", sa.String(), nullable=False, server_default="[]"),
            sa.Column("selected_task_ids", sa.String(), nullable=False, server_default="[]"),
            sa.Column("action_taken", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["task.id"], ),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.drop_table("tasksuggestion")
