"""execution rp_project_used (retry destination pinning, project)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('executions', sa.Column(
        'rp_project_used', sa.String(length=128), nullable=False,
        server_default=''))


def downgrade():
    op.drop_column('executions', 'rp_project_used')
