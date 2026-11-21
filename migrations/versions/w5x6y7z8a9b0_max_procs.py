"""runners: max_procs — combined process budget for parallel jobs

Revision ID: w5x6y7z8a9b0
Revises: v4w5x6y7z8a9
Create Date: 2026-11-20

Additive only. NULL = unconstrained (job slots only). When set, the
atomic assignment CAS also enforces that the sum of requested
parallel processes across active executions on the target plus the
new job's request stays within the budget.
"""
from alembic import op
import sqlalchemy as sa

revision = "w5x6y7z8a9b0"
down_revision = "v4w5x6y7z8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    cols = {c["name"] for c in _inspect(
        op.get_bind()).get_columns("runners")}
    if "max_procs" not in cols:
        op.add_column("runners", sa.Column(
            "max_procs", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("runners", "max_procs")
