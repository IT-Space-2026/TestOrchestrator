"""submit_tokens idempotency table (Run-now duplicate protection)

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-11-04

"""
from alembic import op
import sqlalchemy as sa

revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'


def upgrade():
    op.create_table(
        'submit_tokens',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('schedule_id', sa.String(length=32), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('execution_id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('schedule_id', 'username', 'token',
                            name='uq_submit_tokens'))


def downgrade():
    op.drop_table('submit_tokens')
