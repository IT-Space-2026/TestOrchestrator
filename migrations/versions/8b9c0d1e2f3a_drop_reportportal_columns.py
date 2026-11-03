"""drop ReportPortal columns + active config (scope change)

ReportPortal integration removed — Allure is the sole enhanced
reporting backend. Published historical migrations are preserved; this
FORWARD migration removes the active schema columns and obsolete
settings rows. Shared credentials are NOT deleted — only the
reportportal_* / rp_* settings keys that referenced them.

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-11-04

"""
from alembic import op
import sqlalchemy as sa

revision = '8b9c0d1e2f3a'
down_revision = '7a8b9c0d1e2f'
branch_labels = None
depends_on = None

RP_KEYS = ("reportportal_endpoint", "reportportal_project",
           "reportportal_credential_ref", "reportportal_enabled",
           "rp_launch_name", "rp_verify_tls", "rp_ca_ref")


def upgrade():
    op.drop_column('executions', 'rp_state')
    op.drop_column('executions', 'rp_launch_id')
    op.drop_column('executions', 'rp_endpoint_used')
    op.drop_column('executions', 'rp_project_used')
    op.drop_column('test_results', 'rp_remote_id')
    op.drop_column('artifacts', 'rp_state')
    op.drop_column('artifacts', 'rp_remote_id')
    conn = op.get_bind()
    for key in RP_KEYS:
        conn.execute(sa.text("DELETE FROM settings WHERE key = :k"),
                     {"k": key})


def downgrade():
    # Columns restored nullable/empty — historical data cannot be
    # recovered, matching the "integration removed" semantics.
    op.add_column('artifacts', sa.Column('rp_remote_id', sa.String(64),
                                         nullable=False,
                                         server_default=""))
    op.add_column('artifacts', sa.Column('rp_state', sa.String(16),
                                         nullable=False,
                                         server_default="none"))
    op.add_column('test_results', sa.Column('rp_remote_id',
                                            sa.String(64),
                                            nullable=False,
                                            server_default=""))
    op.add_column('executions', sa.Column('rp_project_used',
                                          sa.String(128),
                                          nullable=False,
                                          server_default=""))
    op.add_column('executions', sa.Column('rp_endpoint_used',
                                          sa.String(512),
                                          nullable=False,
                                          server_default=""))
    op.add_column('executions', sa.Column('rp_launch_id', sa.String(64),
                                          nullable=False,
                                          server_default=""))
    op.add_column('executions', sa.Column('rp_state', sa.String(16),
                                          nullable=False,
                                          server_default="none"))
