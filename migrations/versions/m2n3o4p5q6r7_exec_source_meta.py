"""execution: nullable source_ref + environment snapshots

Revision ID: m2n3o4p5q6r7
Revises: k1b2c3d4e5f6
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = "m2n3o4p5q6r7"
down_revision = "k1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("source_ref", sa.String(length=128), nullable=True))
    op.add_column(
        "executions",
        sa.Column("environment", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "environment")
    op.drop_column("executions", "source_ref")
