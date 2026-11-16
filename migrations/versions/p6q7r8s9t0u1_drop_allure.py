"""drop allure report columns — 2.0 sole-backend removal

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0

Drops allure_status/allure_path/allure_version from executions. These
columns held ONLY Allure-specific report metadata — canonical
execution, result, and artifact rows are untouched (stored report
files on disk are likewise left in place, merely unsupported by the
2.0 interface). reporting-labs (rl_*) columns are the sole backend.
"""
from alembic import op
import sqlalchemy as sa

revision = "p6q7r8s9t0u1"
down_revision = "o5p6q7r8s9t0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("executions", "allure_version")
    op.drop_column("executions", "allure_path")
    op.drop_column("executions", "allure_status")
    # obsolete backend-selection / probe settings rows — inert
    # key-value cleanup; unrelated settings untouched.
    op.execute(
        "DELETE FROM settings WHERE key IN "
        "('allure_enabled', 'allure_probe', 'report_backend')")


def downgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("allure_status", sa.String(16), nullable=False,
                  server_default="pending"))
    op.add_column(
        "executions",
        sa.Column("allure_path", sa.String(512), nullable=False,
                  server_default=""))
    op.add_column(
        "executions",
        sa.Column("allure_version", sa.String(32), nullable=False,
                  server_default=""))
