"""executions: bb_slot — which slot-builder carries this run

Revision ID: x6y7z8a9b0c1
Revises: w5x6y7z8a9b0
Create Date: 2026-11-20

Additive only. Buildbot serializes builds per (builder, worker) pair,
so each runner gets one builder per admitted concurrency slot
(to-target-<id>, to-target-<id>-s2, ...). bb_slot records which slot
builder an execution was submitted to — used for free-slot selection
and reconciliation diagnostics. NULL = not yet submitted.
"""
from alembic import op
import sqlalchemy as sa

revision = "x6y7z8a9b0c1"
down_revision = "w5x6y7z8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    cols = {c["name"] for c in _inspect(
        op.get_bind()).get_columns("executions")}
    if "bb_slot" not in cols:
        op.add_column("executions", sa.Column(
            "bb_slot", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "bb_slot")
