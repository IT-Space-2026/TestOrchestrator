"""schedule multi-targets_json

Revision ID: h8a9b0c1d2e3
Revises: h7a8b9c0d1e2

"""
from alembic import op
import sqlalchemy as sa

revision = 'h8a9b0c1d2e3'
down_revision = 'h7a8b9c0d1e2'


def upgrade():
    with op.batch_alter_table('schedules') as batch:
        batch.add_column(sa.Column('targets_json', sa.Text,
                                   nullable=False, server_default='[]'))


def downgrade():
    with op.batch_alter_table('schedules') as batch:
        batch.drop_column('targets_json')
