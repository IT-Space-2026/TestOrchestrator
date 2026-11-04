"""runner agent_capabilities column (protocol capability gate)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-11-04

"""
from alembic import op
import sqlalchemy as sa

revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'


def upgrade():
    with op.batch_alter_table('runners') as batch:
        batch.add_column(sa.Column(
            'agent_capabilities', sa.String(length=256),
            nullable=False, server_default='[]'))


def downgrade():
    with op.batch_alter_table('runners') as batch:
        batch.drop_column('agent_capabilities')
