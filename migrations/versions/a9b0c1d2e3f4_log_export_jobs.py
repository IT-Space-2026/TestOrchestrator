"""Log export jobs: log_export_jobs table

Revision ID: a9b0c1d2e3f4
Revises: z8a9b0c1d2e3
Create Date: 2026-11-20

Additive only.

- log_export_jobs: background CSV export requests for the Audit Trail
  and Operational Logs. The row survives the generated file so status,
  expiry and audit remain after the file is deleted.
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b0c1d2e3f4"
down_revision = "z8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    if "log_export_jobs" in _inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "log_export_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("filters_json", sa.Text, nullable=False,
                  server_default="{}"),
        sa.Column("cutoff_utc", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("status", sa.String(12), nullable=False,
                  server_default="queued"),
        sa.Column("row_count", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("error", sa.String(256), nullable=False,
                  server_default=""),
        sa.Column("file_name", sa.String(128), nullable=False,
                  server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True),
                  nullable=False),
    )


def downgrade() -> None:
    op.drop_table("log_export_jobs")
