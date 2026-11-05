"""runtime last_check_at

Revision ID: h9a0b1c2d3e4
Revises: h8a9b0c1d2e3

"""
from alembic import op
import sqlalchemy as sa

revision = 'h9a0b1c2d3e4'
down_revision = 'h8a9b0c1d2e3'


def upgrade():
    with op.batch_alter_table('runtime_connections') as batch:
        batch.add_column(sa.Column('last_check_at',
                                   sa.DateTime(timezone=True),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table('runtime_connections') as batch:
        batch.drop_column('last_check_at')
