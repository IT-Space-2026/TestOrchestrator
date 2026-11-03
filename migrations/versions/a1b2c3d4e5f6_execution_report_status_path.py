"""execution report_status + report_path (local-report tracking)

Revision ID: a1b2c3d4e5f6
Revises: f1e2c72bd8a7
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'f1e2c72bd8a7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('executions', sa.Column(
        'report_status', sa.String(length=16), nullable=False,
        server_default='pending'))
    op.add_column('executions', sa.Column(
        'report_path', sa.String(length=512), nullable=False,
        server_default=''))
    op.add_column('executions', sa.Column(
        'allure_status', sa.String(length=16), nullable=False,
        server_default='pending'))
    op.add_column('executions', sa.Column(
        'allure_path', sa.String(length=512), nullable=False,
        server_default=''))
    op.add_column('executions', sa.Column(
        'allure_version', sa.String(length=32), nullable=False,
        server_default=''))


def downgrade():
    op.drop_column('executions', 'allure_version')
    op.drop_column('executions', 'allure_path')
    op.drop_column('executions', 'allure_status')
    op.drop_column('executions', 'report_path')
    op.drop_column('executions', 'report_status')
