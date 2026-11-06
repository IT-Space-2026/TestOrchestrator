"""runtime worker_label (remote install binding)

Revision ID: i0a1b2c3d4e5
Revises: h9a0b1c2d3e4

"""
from alembic import op
import sqlalchemy as sa

revision = 'i0a1b2c3d4e5'
down_revision = 'h9a0b1c2d3e4'


def upgrade():
    with op.batch_alter_table('runtime_connections') as batch:
        batch.add_column(sa.Column('worker_label',
                                   sa.String(128),
                                   nullable=False,
                                   server_default=''))


def downgrade():
    with op.batch_alter_table('runtime_connections') as batch:
        batch.drop_column('worker_label')
