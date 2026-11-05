"""release start/end dates

Revision ID: h7a8b9c0d1e2
Revises: h6a7b8c9d0e1

"""
from alembic import op
import sqlalchemy as sa

revision = 'h7a8b9c0d1e2'
down_revision = 'h6a7b8c9d0e1'


def upgrade():
    with op.batch_alter_table('releases') as batch:
        batch.add_column(sa.Column('start_date', sa.Date, nullable=True))
        batch.add_column(sa.Column('end_date', sa.Date, nullable=True))


def downgrade():
    with op.batch_alter_table('releases') as batch:
        batch.drop_column('end_date')
        batch.drop_column('start_date')
