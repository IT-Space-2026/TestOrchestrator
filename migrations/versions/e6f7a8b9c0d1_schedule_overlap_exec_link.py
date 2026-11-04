"""schedule overlap_policy + executions.schedule_id (run linkage)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-04

"""
from alembic import op
import sqlalchemy as sa

revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'


def upgrade():
    with op.batch_alter_table('schedules') as batch:
        batch.add_column(sa.Column(
            'overlap_policy', sa.String(length=16),
            nullable=False, server_default='allow'))
    with op.batch_alter_table('executions') as batch:
        batch.add_column(sa.Column(
            'schedule_id', sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            'fk_executions_schedule_id', 'schedules',
            ['schedule_id'], ['id'])


def downgrade():
    with op.batch_alter_table('executions') as batch:
        batch.drop_constraint('fk_executions_schedule_id',
                              type_='foreignkey')
        batch.drop_column('schedule_id')
    with op.batch_alter_table('schedules') as batch:
        batch.drop_column('overlap_policy')
