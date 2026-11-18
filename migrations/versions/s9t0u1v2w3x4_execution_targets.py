"""runners: execution-target fields (buildbot worker + agentless ssh)

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-11-18

Additive only. Extends Runner with the target kind and per-type
configuration for the Buildbot orchestration replacement: a unique
buildbot workername + per-worker credential ref for remote workers, and
the SSH connection block (host/port/user/credential ref/approved host-
key fingerprint/workdir) for agentless targets — nothing TO-side runs
on an SSH host. check_* records the last Admin-UI connection probe.
"""
from alembic import op
import sqlalchemy as sa

revision = "s9t0u1v2w3x4"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runners",
        sa.Column("target_type", sa.String(length=20), nullable=False,
                  server_default="local"))
    op.add_column(
        "runners",
        sa.Column("bb_worker_name", sa.String(length=64), nullable=False,
                  server_default=""))
    op.add_column(
        "runners",
        sa.Column("bb_credential_ref", sa.String(length=64),
                  nullable=False, server_default=""))
    op.add_column(
        "runners",
        sa.Column("ssh_host", sa.String(length=256), nullable=False,
                  server_default=""))
    op.add_column("runners", sa.Column("ssh_port", sa.Integer(),
                                       nullable=True))
    op.add_column(
        "runners",
        sa.Column("ssh_username", sa.String(length=64), nullable=False,
                  server_default=""))
    op.add_column(
        "runners",
        sa.Column("ssh_credential_ref", sa.String(length=64),
                  nullable=False, server_default=""))
    op.add_column(
        "runners",
        sa.Column("ssh_host_key_alg", sa.String(length=32),
                  nullable=False, server_default=""))
    op.add_column(
        "runners",
        sa.Column("ssh_host_key_fingerprint", sa.String(length=128),
                  nullable=False, server_default=""))
    op.add_column(
        "runners",
        sa.Column("ssh_workdir", sa.String(length=512), nullable=False,
                  server_default=""))
    op.add_column(
        "runners",
        sa.Column("check_status", sa.String(length=16), nullable=False,
                  server_default=""))
    op.add_column(
        "runners",
        sa.Column("check_error", sa.String(length=512), nullable=False,
                  server_default=""))
    op.add_column("runners", sa.Column(
        "check_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("check_at", "check_error", "check_status", "ssh_workdir",
                "ssh_host_key_fingerprint", "ssh_host_key_alg",
                "ssh_credential_ref", "ssh_username", "ssh_port",
                "ssh_host", "bb_credential_ref", "bb_worker_name",
                "target_type"):
        op.drop_column("runners", col)
