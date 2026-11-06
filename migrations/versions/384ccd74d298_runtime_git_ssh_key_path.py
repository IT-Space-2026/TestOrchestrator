"""runtime git_ssh_key_path

Revision ID: 384ccd74d298
Revises: i0a1b2c3d4e5
Create Date: 2026-11-06 16:26:23.142736

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '384ccd74d298'
down_revision = 'i0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('runtime_connections', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('git_ssh_key_path', sa.String(length=512),
                      nullable=False, server_default=""))
        batch_op.alter_column('git_ssh_key_path', server_default=None)


def downgrade():
    with op.batch_alter_table('runtime_connections', schema=None) as batch_op:
        batch_op.drop_column('git_ssh_key_path')
