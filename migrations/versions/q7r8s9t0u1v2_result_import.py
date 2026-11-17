"""executions: import origin/provenance/dedup + import bookkeeping tables

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-11-16

Additive only — every new executions column has a default so existing
live history is untouched (origin='live', framework='katalon',
fingerprints empty until re-normalised or lazily computed at match
time). The partial unique indexes enforce dedup only where a
fingerprint actually exists (run_uuid != '' / content_hash != ''),
so rows created before fingerprinting never conflict.
"""
from alembic import op
import sqlalchemy as sa

revision = "q7r8s9t0u1v2"
down_revision = "p6q7r8s9t0u1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in (
        sa.Column("origin", sa.String(length=16), nullable=False,
                  server_default="live"),
        sa.Column("framework", sa.String(length=32), nullable=False,
                  server_default="katalon"),
        sa.Column("imported_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("imported_by", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("run_uuid", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("content_hash", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("source_repo", sa.String(length=512), nullable=False,
                  server_default=""),
        sa.Column("source_commit", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("source_path", sa.String(length=512), nullable=False,
                  server_default=""),
    ):
        op.add_column("executions", col)

    # Dedup safety net: the commit-time recheck is the primary gate;
    # these partial indexes make concurrent imports transactional.
    op.create_index(
        "uq_exec_project_run_uuid", "executions",
        ["project_id", "run_uuid"], unique=True,
        sqlite_where=sa.text("run_uuid != ''"))
    op.create_index(
        "uq_exec_project_content_hash", "executions",
        ["project_id", "content_hash"], unique=True,
        sqlite_where=sa.text("content_hash != ''"))

    op.create_table(
        "import_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32),
                  sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("binding_id", sa.String(32),
                  sa.ForeignKey("git_bindings.id"), nullable=False),
        sa.Column("revision_input", sa.String(256), nullable=False,
                  server_default=""),
        sa.Column("resolved_sha", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("result_dir", sa.String(512), nullable=False,
                  server_default=""),
        sa.Column("staging_path", sa.String(512), nullable=False,
                  server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="previewed"),
        sa.Column("summary_json", sa.Text, nullable=False,
                  server_default="{}"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
    )
    op.create_table(
        "import_candidates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32),
                  sa.ForeignKey("import_sessions.id"), nullable=False,
                  index=True),
        sa.Column("seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("run_dir", sa.String(512), nullable=False,
                  server_default=""),
        sa.Column("run_dirs_json", sa.Text, nullable=False,
                  server_default="[]"),
        sa.Column("decision", sa.String(16), nullable=False,
                  server_default="new"),
        sa.Column("match_execution_id", sa.String(32), nullable=False,
                  server_default=""),
        sa.Column("run_uuid", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("content_hash", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("suite_name", sa.String(512), nullable=False,
                  server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("case_count", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("warnings_json", sa.Text, nullable=False,
                  server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("imported_execution_id", sa.String(32), nullable=False,
                  server_default=""),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("import_candidates")
    op.drop_table("import_sessions")
    op.drop_index("uq_exec_project_content_hash", "executions")
    op.drop_index("uq_exec_project_run_uuid", "executions")
    for col in ("source_path", "source_commit", "source_repo",
                "content_hash", "run_uuid", "imported_by",
                "imported_at", "framework", "origin"):
        op.drop_column("executions", col)
