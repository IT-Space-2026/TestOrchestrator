"""report publication + synchronized deletion state on executions

Revision ID: h1a2b3c4d5e6
Revises: h0a1b2c3d4e5

"""
from alembic import op
import sqlalchemy as sa

revision = 'h1a2b3c4d5e6'
down_revision = 'h0a1b2c3d4e5'


def upgrade():
    with op.batch_alter_table('executions') as batch:
        for col, typ in [
                ('report_pub_status', sa.String(16)),
                ('report_pub_repo', sa.String(128)),
                ('report_pub_branch', sa.String(64)),
                ('report_pub_dir', sa.String(512)),
                ('report_pub_sha', sa.String(64)),
                ('report_pub_digest', sa.String(64)),
                ('report_pub_error', sa.String(512)),
                ('deletion_status', sa.String(16)),
                ('deletion_error', sa.String(512))]:
            batch.add_column(sa.Column(col, typ, nullable=True))


def downgrade():
    with op.batch_alter_table('executions') as batch:
        for c in ('deletion_error', 'deletion_status',
                  'report_pub_error', 'report_pub_digest',
                  'report_pub_sha', 'report_pub_dir', 'report_pub_branch',
                  'report_pub_repo', 'report_pub_status'):
            batch.drop_column(c)
