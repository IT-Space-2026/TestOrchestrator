"""authoring drafts: settings_json

Revision ID: h4a5b6c7d8e9
Revises: h3a4b5c6d7e8

"""
from alembic import op
import sqlalchemy as sa

revision = 'h4a5b6c7d8e9'
down_revision = 'h3a4b5c6d7e8'


def upgrade():
    with op.batch_alter_table('authoring_drafts') as batch:
        batch.add_column(sa.Column('settings_json', sa.Text,
                                   nullable=False, server_default='{}'))
        batch.add_column(sa.Column('member_cfg_json', sa.Text,
                                   nullable=False, server_default='{}'))


def downgrade():
    with op.batch_alter_table('authoring_drafts') as batch:
        batch.drop_column('member_cfg_json')
        batch.drop_column('settings_json')
