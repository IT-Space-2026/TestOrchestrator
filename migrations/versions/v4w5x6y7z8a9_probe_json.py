"""runtime_connections: probe_json — last probe markers, machine-readable

Revision ID: v4w5x6y7z8a9
Revises: u3v4w5x6y7z8
Create Date: 2026-11-20

Additive only. The readiness summary text (last_diagnostic) stays
human-readable; probe_json keeps the raw markers so dispatch can gate
optional parallel dependencies (opt_xdist/opt_pabot) on what the
probe actually reported on that target.
"""
from alembic import op
import sqlalchemy as sa

revision = "v4w5x6y7z8a9"
down_revision = "u3v4w5x6y7z8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    cols = {c["name"] for c in _inspect(
        op.get_bind()).get_columns("runtime_connections")}
    if "probe_json" not in cols:
        op.add_column("runtime_connections", sa.Column(
            "probe_json", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("runtime_connections", "probe_json")
