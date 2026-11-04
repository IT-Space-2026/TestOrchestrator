"""source_item ON DELETE SET NULL + binding writable-report flag

Revision ID: h2a3b4c5d6e7
Revises: h1a2b3c4d5e6

"""
from alembic import op
import sqlalchemy as sa

revision = 'h2a3b4c5d6e7'
down_revision = 'h1a2b3c4d5e6'


def upgrade():
    with op.batch_alter_table('executions') as batch:
        batch.drop_constraint('fk_executions_source_item',
                              type_='foreignkey')
        batch.create_foreign_key(
            'fk_executions_source_item', 'discovered_items',
            ['source_item_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('git_bindings') as batch:
        batch.add_column(sa.Column('report_writable', sa.Boolean(),
                                   nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('git_bindings') as batch:
        batch.drop_column('report_writable')
    with op.batch_alter_table('executions') as batch:
        batch.drop_constraint('fk_executions_source_item',
                              type_='foreignkey')
        batch.create_foreign_key(
            'fk_executions_source_item', 'discovered_items',
            ['source_item_id'], ['id'])
