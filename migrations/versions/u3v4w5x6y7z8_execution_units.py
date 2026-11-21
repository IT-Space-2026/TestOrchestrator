"""executions: parent/child unit columns for parallel runs

Revision ID: u3v4w5x6y7z8
Revises: t1u2v3w4x5y6
Create Date: 2026-11-20

Additive only. role/parent_id/unit_index/unit_key/unit_count let a
partitioned run persist a durable parent plus identifiable child
execution units (each with its own frozen assignment in its snapshot).
Existing rows keep role="" — ordinary standalone executions.
"""
from alembic import op
import sqlalchemy as sa

revision = "u3v4w5x6y7z8"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: packaged units run `db-upgrade` concurrently in
    # ExecStartPre — a second starter racing the first must not fail
    # on an already-added column.
    from sqlalchemy import inspect as _inspect
    bind = op.get_bind()
    cols = {c["name"] for c in _inspect(bind).get_columns("executions")}
    if "role" not in cols:
        op.add_column("executions", sa.Column(
            "role", sa.String(16), nullable=False, server_default=""))
    if "parent_id" not in cols:
        op.add_column("executions", sa.Column(
            "parent_id", sa.String(32), nullable=True))
    if "unit_index" not in cols:
        op.add_column("executions", sa.Column(
            "unit_index", sa.Integer(), nullable=True))
    if "unit_key" not in cols:
        op.add_column("executions", sa.Column(
            "unit_key", sa.String(128), nullable=False,
            server_default=""))
    if "unit_count" not in cols:
        op.add_column("executions", sa.Column(
            "unit_count", sa.Integer(), nullable=True))
    idx = {i["name"] for i in _inspect(bind).get_indexes("executions")}
    if "ix_executions_parent_id" not in idx:
        op.create_index("ix_executions_parent_id",
                        "executions", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_executions_parent_id", table_name="executions")
    for name in ("unit_count", "unit_key", "unit_index", "parent_id",
                 "role"):
        op.drop_column("executions", name)
