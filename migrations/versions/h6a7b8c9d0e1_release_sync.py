"""release sync-to-repo state

Revision ID: h6a7b8c9d0e1
Revises: h5a6b7c8d9e0

"""
from alembic import op
import sqlalchemy as sa

revision = 'h6a7b8c9d0e1'
down_revision = 'h5a6b7c8d9e0'


def upgrade():
    with op.batch_alter_table('releases') as batch:
        batch.add_column(sa.Column('sync_binding_id', sa.String(32),
                                   nullable=True))
        batch.add_column(sa.Column('sync_path', sa.String(512),
                                   nullable=False, server_default=''))
        batch.add_column(sa.Column('synced_sha', sa.String(64),
                                   nullable=False, server_default=''))
        batch.add_column(sa.Column('sync_dirty', sa.Boolean,
                                   nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('releases') as batch:
        batch.drop_column('sync_dirty')
        batch.drop_column('synced_sha')
        batch.drop_column('sync_path')
        batch.drop_column('sync_binding_id')
