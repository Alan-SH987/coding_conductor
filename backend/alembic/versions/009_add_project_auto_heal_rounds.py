"""Add auto_heal_rounds column to Project table.

Revision ID: 009
Revises: 008
Create Date: 2026-06-03

Idempotent (same shape as 004): init_db runs SQLModel.create_all first, so on a
fresh DB the column already exists. We only add what's missing, with a server
default so existing rows backfill to 0 (off — auto-heal is opt-in per project).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if "auto_heal_rounds" not in _existing_columns("project"):
        with op.batch_alter_table("project", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "auto_heal_rounds",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("auto_heal_rounds")
