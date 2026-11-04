"""schedules table — durable suite/plan execution schedules

Schedules fire through the worker tick (never request handlers); the
table is authoritative so a restart reconciles missed fire times.

Revision ID: c4d5e6f7a8b9
Revises: 8b9c0d1e2f3a
Create Date: 2026-11-04

"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = '8b9c0d1e2f3a'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'schedules',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('project_id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('target_type', sa.String(length=8), nullable=False),
        sa.Column('suite_id', sa.String(length=32), nullable=True),
        sa.Column('plan_id', sa.String(length=32), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('interval_minutes', sa.Integer(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('run_config_json', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.ForeignKeyConstraint(['suite_id'], ['suites.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('schedules')
