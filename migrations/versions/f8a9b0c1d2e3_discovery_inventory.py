"""discovery inventory table + binding discovery columns

Revision ID: f8a9b0c1d2e3
Revises: f7a8b9c0d1e2

"""
from alembic import op
import sqlalchemy as sa

revision = 'f8a9b0c1d2e3'
down_revision = 'f7a8b9c0d1e2'


def upgrade():
    op.create_table(
        'discovered_items',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('binding_id', sa.String(length=32), nullable=False),
        sa.Column('project_id', sa.String(length=32), nullable=False),
        sa.Column('sha', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('path', sa.String(length=512), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('parent', sa.String(length=512), nullable=False),
        sa.Column('order_idx', sa.Integer(), nullable=False),
        sa.Column('dynamic', sa.Boolean(), nullable=False),
        sa.Column('warnings', sa.Text(), nullable=False),
        sa.Column('members_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['binding_id'], ['git_bindings.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'))
    with op.batch_alter_table('git_bindings') as batch:
        batch.add_column(sa.Column('discovered_sha', sa.String(64),
                                   nullable=False, server_default=''))
        batch.add_column(sa.Column('discovery_status', sa.String(16),
                                   nullable=False,
                                   server_default='pending'))
        batch.add_column(sa.Column('discovery_at',
                                   sa.DateTime(timezone=True),
                                   nullable=True))
        batch.add_column(sa.Column('discovery_error', sa.String(512),
                                   nullable=False, server_default=''))
        batch.add_column(sa.Column('project_root', sa.String(256),
                                   nullable=False, server_default=''))


def downgrade():
    with op.batch_alter_table('git_bindings') as batch:
        for c in ('project_root', 'discovery_error', 'discovery_at',
                  'discovery_status', 'discovered_sha'):
            batch.drop_column(c)
    op.drop_table('discovered_items')
