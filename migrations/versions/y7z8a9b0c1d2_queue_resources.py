"""queue policies: wait reasons, queue deadlines, misfire policy,
resource_reservations ledger

Revision ID: y7z8a9b0c1d2
Revises: x6y7z8a9b0c1
Create Date: 2026-11-20

Additive only.

- executions.wait_reason: actionable cause while a run stays QUEUED
  (which capacity/policy gate blocks it) — never a silent stall.
- executions.queue_deadline_at: optional deadline; a QUEUED run past
  it fails with an explicit queue reason instead of waiting forever.
- schedules.misfire_policy: 'late' (current behaviour — fire once late)
  or 'skip' (drop an occurrence overdue past its interval).
- resource_reservations: durable ownership ledger written atomically
  at assignment — who holds which runner slot / how many procs, from
  when to when. Released only on a confirmed terminal state; UNKNOWN
  executions keep their reservation open.
"""
from alembic import op
import sqlalchemy as sa

revision = "y7z8a9b0c1d2"
down_revision = "x6y7z8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as _inspect
    bind = op.get_bind()
    cols = {c["name"] for c in _inspect(bind).get_columns("executions")}
    if "wait_reason" not in cols:
        op.add_column("executions", sa.Column(
            "wait_reason", sa.String(256), nullable=False,
            server_default=""))
    if "queue_deadline_at" not in cols:
        op.add_column("executions", sa.Column(
            "queue_deadline_at", sa.DateTime(timezone=True),
            nullable=True))
    scols = {c["name"] for c in _inspect(bind).get_columns("schedules")}
    if "misfire_policy" not in scols:
        op.add_column("schedules", sa.Column(
            "misfire_policy", sa.String(16), nullable=False,
            server_default="late"))
    if "resource_reservations" not in _inspect(bind).get_table_names():
        op.create_table(
            "resource_reservations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("execution_id", sa.String(32),
                      sa.ForeignKey("executions.id"), nullable=False),
            sa.Column("runner_id", sa.String(32), nullable=False),
            sa.Column("resource_kind", sa.String(16), nullable=False),
            sa.Column("slot_index", sa.Integer(), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("acquired_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("release_state", sa.String(32), nullable=False,
                      server_default=""),
        )
        op.create_index("ix_resource_reservations_execution",
                        "resource_reservations", ["execution_id"])
        op.create_index("ix_resource_reservations_runner",
                        "resource_reservations", ["runner_id",
                                                  "released_at"])


def downgrade() -> None:
    op.drop_index("ix_resource_reservations_runner")
    op.drop_index("ix_resource_reservations_execution")
    op.drop_table("resource_reservations")
    op.drop_column("schedules", "misfire_policy")
    op.drop_column("executions", "queue_deadline_at")
    op.drop_column("executions", "wait_reason")
