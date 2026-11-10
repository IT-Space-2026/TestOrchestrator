"""Runtime worker-local credential file path

Revision ID: n4o5p6q7r8s9
Revises: m2n3o4p5q6r7

Adds credential_file to runtime_connections — an admin-configured,
worker-local path the Dagu execution DAG references by name only.
The worker-side resolver (scripts/kre_cred_exec.py) reads the value
on the worker immediately before exec; the coordinator spec carries
the path, never the secret.
"""
from alembic import op
import sqlalchemy as sa

revision = "n4o5p6q7r8s9"
down_revision = "m2n3o4p5q6r7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_connections",
        sa.Column("credential_file", sa.String(512), nullable=False,
                  server_default=""),
    )


def downgrade() -> None:
    op.drop_column("runtime_connections", "credential_file")
