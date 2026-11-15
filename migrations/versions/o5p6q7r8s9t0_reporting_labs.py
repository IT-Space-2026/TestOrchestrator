"""reporting-labs backend status columns

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9

Adds rl_status/rl_path/rl_version to executions — the reporting-labs
backend's independent report status (2.0 migration; Allure columns are
untouched and remain the default backend).
"""
from alembic import op
import sqlalchemy as sa

revision = "o5p6q7r8s9t0"
down_revision = "n4o5p6q7r8s9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("rl_status", sa.String(16), nullable=False,
                  server_default="pending"))
    op.add_column(
        "executions",
        sa.Column("rl_path", sa.String(512), nullable=False,
                  server_default=""))
    op.add_column(
        "executions",
        sa.Column("rl_version", sa.String(32), nullable=False,
                  server_default=""))


def downgrade() -> None:
    op.drop_column("executions", "rl_version")
    op.drop_column("executions", "rl_path")
    op.drop_column("executions", "rl_status")
