"""release scope items + executions.release_id

Revision ID: h5a6b7c8d9e0
Revises: h4a5b6c7d8e9

"""
from alembic import op
import sqlalchemy as sa

revision = 'h5a6b7c8d9e0'
down_revision = 'h4a5b6c7d8e9'


def upgrade():
    op.create_table(
        'release_items',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('release_id', sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(['release_id'], ['releases.id'],
                                name='fk_release_items_release'),
        sa.Column('kind', sa.String(16), nullable=False),   # suite|collection
        sa.Column('path', sa.String(512), nullable=False),  # source-relative
        sa.Column('source_item_id', sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(['source_item_id'],
                                ['discovered_items.id'],
                                name='fk_release_items_source',
                                ondelete='SET NULL'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('executions') as batch:
        batch.add_column(sa.Column(
            'release_id', sa.String(32), nullable=True))
        batch.create_foreign_key(
            'fk_executions_release', 'releases',
            ['release_id'], ['id'])


def downgrade():
    with op.batch_alter_table('executions') as batch:
        batch.drop_column('release_id')
    op.drop_table('release_items')
