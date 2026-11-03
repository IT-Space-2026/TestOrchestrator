"""runner license_slots — declared KRE licence allowance

Revision ID: 7a8b9c0d1e2f
Revises: 2296aff581c6
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa

revision = '7a8b9c0d1e2f'
down_revision = '2296aff581c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('runners',
                  sa.Column('license_slots', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('runners', 'license_slots')
