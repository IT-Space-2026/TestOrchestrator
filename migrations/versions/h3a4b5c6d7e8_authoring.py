"""authoring: writable flag + drafts table

Revision ID: h3a4b5c6d7e8
Revises: h2a3b4c5d6e7

"""
from alembic import op
import sqlalchemy as sa

revision = 'h3a4b5c6d7e8'
down_revision = 'h2a3b4c5d6e7'


def upgrade():
    with op.batch_alter_table('git_bindings') as batch:
        batch.add_column(sa.Column('authoring_writable', sa.Boolean(),
                                   nullable=False, server_default='0'))
        batch.add_column(sa.Column('authoring_branch', sa.String(64),
                                   nullable=True))
    op.create_table(
        'authoring_drafts',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('binding_id', sa.String(32),
                  sa.ForeignKey('git_bindings.id'), nullable=False),
        sa.Column('project_id', sa.String(32), nullable=False),
        sa.Column('owner', sa.String(128), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),   # suite|collection
        sa.Column('path', sa.String(512), nullable=False),  # source-relative
        sa.Column('base_sha', sa.String(64), nullable=False, default=""),
        sa.Column('members_json', sa.Text, nullable=False, default="[]"),
        sa.Column('name', sa.String(256), nullable=False, default=""),
        sa.Column('status', sa.String(16), nullable=False,
                  default="draft"),   # draft|published|abandoned
        sa.Column('published_sha', sa.String(64), nullable=False,
                  default=""),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_table('authoring_drafts')
    with op.batch_alter_table('git_bindings') as batch:
        batch.drop_column('authoring_branch')
        batch.drop_column('authoring_writable')
