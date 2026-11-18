"""Agentless-SSH target verification (docs/orchestration-replacement.md §5).

``check_target`` runs the staged Admin-UI "Test SSH Connection": TCP
reachability -> host-key verification -> authentication -> remote
working-directory writability -> SFTP -> per-runtime executable checks.
Each stage reports independently so the UI can name the failed check
without exposing secrets or key material.

Host identity is TOFU-with-approval: an unverified host returns its
discovered fingerprint for explicit admin approval (``approve_host_key``
persists it); a stored fingerprint that no longer matches is a hard
failure requiring re-verification — never silently re-pinned.

Nothing is installed and no test execution is launched — probes are
read-only apart from a create-and-remove marker inside the configured
workdir.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field

from flask import current_app


@dataclass
class CheckResult:
    status: str = "ok"             # ok | fail | unverified
    stage: str = ""                # failing/verifying stage
    error: str = ""
    fingerprint: str = ""          # discovered host-key fingerprint
    key_alg: str = ""
    detail: str = ""
    stages: list = field(default_factory=list)

    def fail(self, stage: str, error: str) -> "CheckResult":
        self.status, self.stage, self.error = "fail", stage, error
        self.stages.append((stage, "fail"))
        return self

    def passed(self, stage: str) -> None:
        self.stages.append((stage, "ok"))


def _fingerprint(key) -> tuple[str, str]:
    """(algorithm, SHA256 fingerprint) for a paramiko PKey."""
    import base64
    import hashlib
    digest = hashlib.sha256(key.asbytes()).digest()
    return key.get_name(), "SHA256:" + base64.b64encode(
        digest).decode().rstrip("=")


def _load_key(value: str):
    """Parse stored private-key material into a paramiko PKey."""
    import io
    import paramiko
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey,
                paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(value))
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError("stored credential is not a usable SSH private key")


def probe_host_key(host: str, port: int,
                   timeout: int = 10) -> tuple[str, str]:
    """Fetch the host key without authenticating — used both by the
    check and by the explicit approval step. Returns (alg, fp)."""
    import paramiko
    transport = paramiko.Transport(socket.create_connection(
        (host, port), timeout=timeout))
    try:
        transport.start_client(timeout=timeout)
        return _fingerprint(transport.get_remote_server_key())
    finally:
        transport.close()


def check_target(runner, runtimes=None) -> CheckResult:
    """Staged verification for a target_type="ssh" runner row."""
    res = CheckResult()
    host = (runner.ssh_host or "").strip()
    port = runner.ssh_port or 22
    if not host or not runner.ssh_username:
        return res.fail("configuration",
                        "host and SSH username are required")

    try:
        alg, fp = probe_host_key(host, port)
    except (OSError, Exception) as exc:  # noqa: BLE001 — report stage
        if isinstance(exc, (socket.timeout, TimeoutError,
                            ConnectionRefusedError, socket.gaierror,
                            OSError)):
            return res.fail("reachability",
                            f"{host}:{port} unreachable — {exc}")
        return res.fail("reachability", f"SSH handshake failed — {exc}")
    res.passed("reachability")
    res.fingerprint, res.key_alg = fp, alg

    stored = (runner.ssh_host_key_fingerprint or "").strip()
    if not stored:
        res.status = "unverified"
        res.stage = "host-key"
        res.error = ("host key not yet approved — review the "
                     "fingerprint and approve it")
        res.stages.append(("host-key", "unverified"))
        return res
    if stored != fp or (runner.ssh_host_key_alg and
                        runner.ssh_host_key_alg != alg):
        return res.fail(
            "host-key",
            f"host key changed — expected {stored}, got {fp}. "
            "Verify the host and re-approve the new key.")
    res.passed("host-key")

    try:
        import paramiko
        from .secrets_store import read_secret
        value = read_secret(current_app._get_current_object(),
                            runner.ssh_credential_ref)
        pkey = _load_key(value)
    except Exception as exc:
        return res.fail("credential",
                        f"SSH credential unusable — {exc}")
    res.passed("credential")

    try:
        sock = socket.create_connection((host, port), timeout=10)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=10)
        # Verify the presented key against the pinned fingerprint
        # AGAIN at connect time — the earlier probe and this session
        # could be answered differently by a MITM.
        alg2, fp2 = _fingerprint(transport.get_remote_server_key())
        if fp2 != stored:
            transport.close()
            return res.fail(
                "host-key",
                f"host key changed mid-check — {fp2} != pinned "
                f"{stored}")
        transport.auth_publickey(runner.ssh_username, pkey)
    except paramiko.AuthenticationException:
        return res.fail("authentication",
                        "authentication refused — check username/key")
    except paramiko.SSHException as exc:
        return res.fail("authentication", f"SSH auth failed — {exc}")
    res.passed("authentication")

    try:
        workdir = (runner.ssh_workdir or "").strip() or "~/to-runs"
        # Create a marker inside the base dir and remove it — verifies
        # writability without leaving residue.
        probe = f"{workdir}/.to-write-test"
        chan = transport.open_session(timeout=15)
        chan.exec_command(
            f"mkdir -p {workdir!r} && touch {probe!r} && "
            f"rm -f {probe!r}")
        err = chan.makefile_stderr("rb").read().decode(
            "utf-8", "replace")
        if chan.recv_exit_status() != 0:
            return res.fail("working-directory",
                            f"{workdir} not writable — {err[:200]}")
        chan.close()
        res.passed("working-directory")

        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.close()
        res.passed("sftp")

        for rt in (runtimes or []):
            exe = (rt.executable_path or "").strip()
            if not exe:
                continue
            chan = transport.open_session(timeout=10)
            chan.exec_command(f"test -x {exe!r}")
            ok = chan.recv_exit_status() == 0
            chan.close()
            if not ok:
                return res.fail(
                    "runner",
                    f"{rt.name}: {exe} not executable on the host")
        res.passed("runner")
    finally:
        transport.close()
    return res
