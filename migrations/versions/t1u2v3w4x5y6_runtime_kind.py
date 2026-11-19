"""runtime_connections: runtime_kind for node test frameworks

Revision ID: t1u2v3w4x5y6
Revises: s9t0u1v2w3x4
Create Date: 2026-11-19

Additive only. runtime_kind distinguishes the KRE runtime ("kre",
default — every existing row) from the new Node.js browser-test
runtimes ("playwright", "cypress"). Selection, dispatch and
collection reuse the existing Buildbot paths; the kind only changes
how argv/env are rendered and which validations apply.
"""
from alembic import op
import sqlalchemy as sa

revision = "t1u2v3w4x5y6"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_connections",
        sa.Column("runtime_kind", sa.String(length=16), nullable=False,
                  server_default="kre"))


def downgrade() -> None:
    op.drop_column("runtime_connections", "runtime_kind")
