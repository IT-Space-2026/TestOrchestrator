"""discovered_items.members_json — forward path for DBs already at
f8a9b0c1d2e3 before the column was added

Revision ID: g9a0b1c2d3e4
Revises: f8a9b0c1d2e3

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'g9a0b1c2d3e4'
down_revision = 'f8a9b0c1d2e3'


def upgrade():
    conn = op.get_bind()
    cols = {c["name"] for c in
            inspect(conn).get_columns("discovered_items")}
    if "members_json" not in cols:
        with op.batch_alter_table('discovered_items') as batch:
            batch.add_column(sa.Column('members_json', sa.Text(),
                                       nullable=False, server_default='[]'))


def downgrade():
    pass   # safe no-op: do not drop a column the base rev may have added
