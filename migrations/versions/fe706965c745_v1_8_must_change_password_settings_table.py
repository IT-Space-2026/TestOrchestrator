"""v1.8: must_change_password, settings table

Revision ID: fe706965c745
Revises: 6ea754d7d782
Create Date: 2026-11-03 07:40:19.137235

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fe706965c745'
down_revision = '6ea754d7d782'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('key')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        # Existing rows become must_change_password=0; the bootstrap account
        # sets it explicitly. Model default is client-side.
        batch_op.add_column(
            sa.Column('must_change_password', sa.Boolean(),
                      nullable=False, server_default=sa.false())
        )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('must_change_password')
    op.drop_table('settings')
