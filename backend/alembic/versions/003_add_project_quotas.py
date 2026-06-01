"""Add quota_tokens and quota_cost_usd columns to Project table.

Revision ID: 003
Revises: 002
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
revision: str = "003"
down_revision: Union[str, None] = "002"
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
    if "quota_tokens" not in project_cols:
        to_add.append(sa.Column("quota_tokens", sa.Integer(), nullable=True))
    if "quota_cost_usd" not in project_cols:
        to_add.append(sa.Column("quota_cost_usd", sa.Float(), nullable=True))
    if to_add:
        with op.batch_alter_table("project", schema=None) as batch_op:
            for col in to_add:
                batch_op.add_column(col)


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("quota_cost_usd")
        batch_op.drop_column("quota_tokens")
