"""execution rp_endpoint_used (retry destination pinning)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('executions', sa.Column(
        'rp_endpoint_used', sa.String(length=512), nullable=False,
        server_default=''))


def downgrade():
    op.drop_column('executions', 'rp_endpoint_used')
