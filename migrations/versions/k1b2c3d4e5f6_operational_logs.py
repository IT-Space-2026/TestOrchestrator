"""operational_logs table — Admin -> Operational Logs"""
from alembic import op
import sqlalchemy as sa

revision = "k1b2c3d4e5f6"
down_revision = "384ccd74d298"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operational_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("severity", sa.String(12), nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(64)),
        sa.Column("execution_id", sa.String(64)),
        sa.Column("worker_id", sa.String(64)),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("message", sa.String(512), nullable=False),
        sa.Column("details", sa.Text))
    for col in ("occurred_at", "severity", "component", "project_id",
                "execution_id", "worker_id", "correlation_id"):
        op.create_index(f"ix_operational_logs_{col}",
                        "operational_logs", [col])


def downgrade():
    op.drop_table("operational_logs")
