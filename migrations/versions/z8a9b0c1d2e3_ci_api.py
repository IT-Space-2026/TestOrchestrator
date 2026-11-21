"""CI API + webhooks: api_tokens, webhook_endpoints, execution
client_ref / notified_at

Revision ID: z8a9b0c1d2e3
Revises: y7z8a9b0c1d2
Create Date: 2026-11-20

Additive only.

- api_tokens: CI/CD bearer credentials (SHA-256 stored; value shown
  once). scope read|run; allowed_projects mirrors user scoping.
- webhook_endpoints: outbound notification subscriptions delivered
  via the durable outbox with backoff; secret_ref names the HMAC key
  in the encrypted secret store — never inline.
- executions.client_ref: API idempotency key — a repeated POST with
  the same (token, client_ref) returns the existing execution.
- executions.notified_at: durable fan-out marker — set once webhook
  outbox rows exist for the execution's terminal/UNKNOWN state, so a
  crash between transition and enqueue retries rather than drops.
"""
from alembic import op
import sqlalchemy as sa

revision = "z8a9b0c1d2e3"
down_revision = "y7z8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    bind = op.get_bind()
    cols = {c["name"] for c in _inspect(bind).get_columns("executions")}
    if "client_ref" not in cols:
        op.add_column("executions", sa.Column(
            "client_ref", sa.String(128), nullable=False,
            server_default=""))
        op.create_index("ix_executions_client_ref",
                        "executions", ["client_ref"])
    if "notified_at" not in cols:
        op.add_column("executions", sa.Column(
            "notified_at", sa.DateTime(timezone=True), nullable=True))
        # Backfill: rows already terminal/UNKNOWN at upgrade predate the
        # feature — marking them notified prevents a stale-event storm
        # against the first endpoint ever created. autocommit_block is
        # required: this DB runs with transactional_ddl=False, so a
        # plain op.execute DML statement never reaches commit.
        with op.get_context().autocommit_block():
            op.execute(
                "UPDATE executions SET notified_at = CURRENT_TIMESTAMP "
                "WHERE state IN ('COMPLETED','FAILED','CANCELED',"
                "'UNKNOWN')")
    tables = _inspect(bind).get_table_names()
    if "api_tokens" not in tables:
        op.create_table(
            "api_tokens",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False,
                      unique=True),
            sa.Column("scope", sa.String(16), nullable=False,
                      server_default="read"),
            sa.Column("allowed_projects", sa.Text, nullable=False,
                      server_default="[]"),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True),
                      nullable=True),
        )
    if "webhook_endpoints" not in tables:
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("url", sa.String(1024), nullable=False),
            sa.Column("events", sa.String(256), nullable=False,
                      server_default="completed,failed,canceled"),
            sa.Column("secret_ref", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("enabled", sa.Boolean, nullable=False,
                      server_default="1"),
            sa.Column("project_scope", sa.Text, nullable=False,
                      server_default="[]"),
            sa.Column("created_by", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=True),
        )


def downgrade() -> None:
    op.drop_table("webhook_endpoints")
    op.drop_table("api_tokens")
    op.drop_column("executions", "notified_at")
    op.drop_index("ix_executions_client_ref")
    op.drop_column("executions", "client_ref")
