"""executions: nullable suite_id + source_item link (direct repo-def runs)

Revision ID: h0a1b2c3d4e5
Revises: g9a0b1c2d3e4

"""
from alembic import op
import sqlalchemy as sa

revision = 'h0a1b2c3d4e5'
down_revision = 'g9a0b1c2d3e4'


def upgrade():
    with op.batch_alter_table('executions') as batch:
        batch.alter_column('suite_id', existing_type=sa.String(32),
                           nullable=True)
        batch.add_column(sa.Column(
            'source_item_id', sa.String(32), nullable=True))
        batch.create_foreign_key(
            'fk_executions_source_item', 'discovered_items',
            ['source_item_id'], ['id'])


def downgrade():
    with op.batch_alter_table('executions') as batch:
        batch.drop_constraint('fk_executions_source_item',
                              type_='foreignkey')
        batch.drop_column('source_item_id')
        batch.alter_column('suite_id', existing_type=sa.String(32),
                           nullable=False)
