"""executions.suite_keys_json + import_locks (2.0.2 import hardening)

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-11-17

Additive only. suite_keys_json persists parsed suite keys so dedup's
suite/timestamp overlap check runs in memory; NULL = never
fingerprinted (lazy path stamps it once), '[]' = fingerprinted with
no readable evidence. import_locks serializes concurrent confirms —
content_hash is evidence, not identity, so uuid-less concurrent
imports of identical content need an application-level arbiter.
"""
from alembic import op
import sqlalchemy as sa

revision = "r8s9t0u1v2w3"
down_revision = "q7r8s9t0u1v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("suite_keys_json", sa.Text(), nullable=True))
    op.create_table(
        "import_locks",
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("claimed_by", sa.String(length=64), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.PrimaryKeyConstraint("project_id"))


def downgrade() -> None:
    op.drop_table("import_locks")
    op.drop_column("executions", "suite_keys_json")
