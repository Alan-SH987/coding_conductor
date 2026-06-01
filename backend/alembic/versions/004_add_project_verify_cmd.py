"""Add verify_cmd column to Project table.

Revision ID: 004
Revises: 003
Create Date: 2026-06-02

Idempotent (same shape as 003): init_db runs SQLModel.create_all first, so on a
fresh DB the column already exists. We only add what's missing, so the revision
works whether the DB pre-dates the column or was just created by create_all.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if "verify_cmd" not in _existing_columns("project"):
        with op.batch_alter_table("project", schema=None) as batch_op:
            batch_op.add_column(sa.Column("verify_cmd", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("verify_cmd")
