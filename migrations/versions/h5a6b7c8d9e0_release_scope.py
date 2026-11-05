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
        sa.Column('release_id', sa.String(32),
                  sa.ForeignKey('releases.id'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),   # suite|collection
        sa.Column('path', sa.String(512), nullable=False),  # source-relative
        sa.Column('source_item_id', sa.String(32),
                  sa.ForeignKey('discovered_items.id',
                                ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table('executions') as batch:
        batch.add_column(sa.Column(
            'release_id', sa.String(32),
            sa.ForeignKey('releases.id'), nullable=True))


def downgrade():
    with op.batch_alter_table('executions') as batch:
        batch.drop_column('release_id')
    op.drop_table('release_items')
