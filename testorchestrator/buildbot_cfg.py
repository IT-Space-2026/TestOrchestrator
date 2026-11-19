"""Portal-side Buildbot configuration (docs/orchestration-replacement.md).

The portal owns the buildmaster's dynamic surface: every Admin-UI target
change rewrites ``targets.json`` (routing/builders, no secrets) and
``secrets.json`` (0600, API + worker passwords) under the master dir,
then signals ``buildbot reconfig`` via the recorded pidfile. Saved
settings take effect with no manual controller configuration and no
restart — master.cfg re-reads both files on SIGHUP.

Secrets discipline: secrets.json is the only file carrying resolved
credential values; it is written 0600 inside the protected master dir.
SSH key material stays in the secrets store — run specs reference the
0600 key FILE the portal materializes, never key bytes.
"""

import json
import os
import tarfile
from pathlib import Path

from flask import current_app

from . import settings as settings_mod
from .execution import Runner
from .secrets_store import read_secret

LOCAL_WORKER = "to-local"
SSH_DISPATCHER = "to-ssh-dispatcher"


class BuildbotConfigError(RuntimeError):
    pass


# ---------- paths ----------

def home() -> Path:
    h = settings_mod.get("buildbot_home") or ""
    if h:
        return Path(h)
    return Path(current_app.instance_path) / "buildbot"


def staging_dir() -> Path:
    return home() / "staging"


def artifacts_dir() -> Path:
    return home() / "artifacts"


def run_specs_dir() -> Path:
    return home() / "run-specs"


def _write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, mode)
    tmp.replace(path)


# ---------- naming ----------

def builder_for(runner: Runner) -> str:
    return (f"to-ssh-{runner.id}" if runner.target_type == "ssh"
            else f"to-target-{runner.id}")


def worker_for(runner: Runner) -> str:
    if runner.target_type == "ssh":
        return SSH_DISPATCHER
    if runner.target_type == "buildbot-worker":
        return runner.bb_worker_name
    return LOCAL_WORKER


def ensure_worker_name(runner: Runner) -> str:
    """Deterministic unique workername for a buildbot-worker target —
    identity survives edit/reinstall (reinstall must not duplicate)."""
    if not runner.bb_worker_name:
        runner.bb_worker_name = f"bbw-{runner.id}"
    return runner.bb_worker_name


# ---------- secrets ----------

def _api_secret() -> str:
    ref = settings_mod.get("buildbot_credential_ref") or ""
    return read_secret(current_app._get_current_object(), ref) if ref \
        else ""


def _worker_secret(runner: Runner) -> str:
    return (read_secret(current_app._get_current_object(),
                        runner.bb_credential_ref)
            if runner.bb_credential_ref else "")


def write_targets_config(app=None) -> dict:
    """Enumerate portal targets -> targets.json + secrets.json. Called
    after every target create/edit/enable/disable/remove."""
    targets, workers = [], {}
    # The two controller-local workers always exist — local KRE
    # dispatch and the SSH dispatcher both need them registered.
    workers[LOCAL_WORKER] = {"name": LOCAL_WORKER}
    workers[SSH_DISPATCHER] = {"name": SSH_DISPATCHER}
    passwords = {}
    for ref, val in _internal_worker_passwords().items():
        passwords[ref] = val

    for r in Runner.query.filter(Runner.target_type != "",
                                 Runner.enabled.is_(True)).all():
        if r.target_type == "buildbot-worker":
            name = ensure_worker_name(r)
            workers[name] = {"name": name}
            sec = _worker_secret(r)
            if sec:
                passwords[name] = sec
        targets.append({
            "id": r.id,
            "builder": builder_for(r),
            "worker": worker_for(r),
            "kind": ("ssh" if r.target_type == "ssh" else "kre"),
            "enabled": True,
        })
    _write_json(home() / "targets.json", {
        "workers": list(workers.values()),
        "targets": targets,
        "staging_dir": str(staging_dir()),
        "artifacts_dir": str(artifacts_dir()),
        "pb_port": f"tcp:{settings_mod.get('buildbot_pb_port', 9989)}"
                   ":interface=0.0.0.0",
        "www_port": f"tcp:{settings_mod.get('buildbot_www_port', 8010)}"
                    ":interface=127.0.0.1",
    })
    _write_json(home() / "secrets.json", {
        "api_user": settings_mod.get("buildbot_username") or "to-api",
        "api_password": _api_secret(),
        "worker_passwords": passwords,
    })
    return {"workers": len(workers), "targets": len(targets)}


def _internal_worker_passwords() -> dict:
    """Controller-local worker credentials — generated once, kept in the
    secrets store under fixed refs so reruns preserve them (same rule
    as the coordinator credential)."""
    app = current_app._get_current_object()
    from .secrets_store import SecretError
    from .extensions import db
    import secrets as _secrets
    out = {}
    for name in (LOCAL_WORKER, SSH_DISPATCHER):
        ref = f"buildbot-worker-{name}"
        try:
            out[name] = read_secret(app, ref)
        except SecretError:
            from .secrets_store import store_secret
            val = "bb-" + _secrets.token_urlsafe(24)
            store_secret(app, ref, "buildbot", val,
                         {"purpose": f"internal worker {name}",
                          "auto": True})
            out[name] = val
    db.session.commit()
    return out


def trigger_reconfig() -> bool:
    """Make the master re-read targets/secrets.

    POSIX: SIGHUP the recorded master pid (no restart).
    Windows: SIGHUP does not exist — end + run the master Scheduled
    Task instead. A brief restart on a rare admin action is the only
    correct reconfig mechanism there."""
    import signal
    if os.name == "nt":
        import subprocess
        try:
            subprocess.run(
                ["schtasks", "/End", "/TN",
                 "TestOrchestratorBuildbotMaster"],
                capture_output=True, timeout=30)
            import time
            time.sleep(2)
            r = subprocess.run(
                ["schtasks", "/Run", "/TN",
                 "TestOrchestratorBuildbotMaster"],
                capture_output=True, timeout=30)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    pidfile = settings_mod.get("buildbot_pidfile") or ""
    pid = _master_pid(pidfile)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except OSError:
        return False


def _master_pid(pidfile: str) -> int | None:
    """Resolve the running master pid. twistd only writes its pidfile
    when daemonizing — the shipped unit runs ``--nodaemon`` under
    systemd, so fall back to the service manager's MainPID, then a
    /proc scan for a process whose cmdline names the master dir."""
    if pidfile:
        try:
            pid = int(Path(pidfile).read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            pass
    import subprocess
    for unit in ("testorchestrator-buildbot-master.service",):
        for scope in ("--user", ""):
            try:
                out = subprocess.run(
                    ["systemctl", *([scope] if scope else []),
                     "show", "-p", "MainPID", "--value", unit],
                    capture_output=True, text=True, timeout=5)
                pid = int(out.stdout.strip() or 0)
                if pid > 0:
                    return pid
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
    home = settings_mod.get("buildbot_home") or ""
    if home:
        master = str(Path(home) / "master")
        proc = Path("/proc")
        if proc.is_dir():
            for p in proc.iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    cmd = (p / "cmdline").read_bytes().replace(
                        b"\0", b" ").decode(errors="replace")
                    if "buildbot" in cmd and master in cmd:
                        return int(p.name)
                except (OSError, ValueError):
                    continue
    return None


def sync() -> dict:
    """Rewrite config files + signal reconfig — the single call every
    target mutation makes."""
    info = write_targets_config()
    info["reconfigured"] = trigger_reconfig()
    return info


# ---------- staging & specs ----------

def stage_source(ex, source_dir: Path) -> Path:
    """Tar the resolved source for a buildrequest — FileDownload pushes
    it to the worker; nothing on the remote needs git."""
    staging_dir().mkdir(parents=True, exist_ok=True)
    dest = staging_dir() / f"{ex.id}.tar"
    tmp = dest.with_suffix(".tar.tmp")
    # Binding-less suite runs have no worktree — same tolerance as
    # kre_adapter.prepare_workspace: an empty project root, not a
    # submission failure.
    empty = None
    if not source_dir.exists():
        import tempfile
        empty = Path(tempfile.mkdtemp(prefix="bb-empty-"))
        (empty / "src").mkdir()
        source_dir = empty / "src"
    with tarfile.open(tmp, "w") as tar:
        tar.add(source_dir, arcname="src")
    tmp.replace(dest)
    return dest


def clear_staging(execution_id: str) -> None:
    for p in (staging_dir() / f"{execution_id}.tar",
              staging_dir() / f"{execution_id}.cred",
              staging_dir() / f"{execution_id}.tar.tmp"):
        try:
            p.unlink()
        except OSError:
            pass


def kre_exec_spec(ex, runtime, args: list,
                  credential_file: str = "") -> dict:
    """Validated runner spec for local + remote worker targets — argv
    only, never a shell string and never secrets. ``credential_file``
    is a PATH: worker-local for remote targets, a portal-written 0600
    file under staging for the controller-local worker."""
    spec = {
        "executable": runtime.executable_path,
        "args": list(args),
        # ShellCommand.workdir resolves relative to the worker BASEDIR
        # (its default is "build") — the staged tar unpacks src/ under
        # the builddir, so the exec workdir must say "build/src".
        "exec_workdir": "build/src",
        # DirectoryUpload.workersrc resolves relative to the BUILDDIR.
        "report_src": "src/Reports",
    }
    wrapper = str(
        Path(__file__).resolve().parent.parent / "scripts"
        / "kre_cred_exec.py")
    if credential_file:
        spec["credential_wrapper"] = wrapper
        spec["credential_file"] = credential_file
    return spec


def write_run_credential(execution_id: str, value: str) -> str:
    """Controller-local runs (to-local worker): portal resolves the
    runtime credential to a per-run 0600 file the wrapper reads — the
    value never enters properties/argv/logs."""
    staging_dir().mkdir(parents=True, exist_ok=True)
    path = staging_dir() / f"{execution_id}.cred"
    path.write_text(value)
    os.chmod(path, 0o600)
    return str(path)


def node_exec_spec(ex, executable: str, args: list,
                   env: dict = None, deps_argv: list = None) -> dict:
    """Validated runner spec for Node.js browser-test runtimes
    (playwright/cypress). Same contract as kre_exec_spec — argv list
    only, no shell, no secrets — plus an explicit env map (e.g.
    PLAYWRIGHT_JUNIT_OUTPUT_FILE) rendered into the ShellCommand.
    No credential wrapper: these frameworks need no TO licence key.
    ``deps_argv`` is an optional pre-exec step (``npm ci`` — the
    project's pinned dependencies into the isolated workspace)."""
    spec = {
        "executable": executable,
        "args": list(args),
        # tar unpacks the project to <builddir>/src — the framework
        # CLI runs inside it (project-pinned deps via npx).
        "exec_workdir": "build/src",
        "report_src": "src/Reports",
    }
    if env:
        spec["env"] = dict(env)
    if deps_argv:
        spec["deps_argv"] = list(deps_argv)
    return spec


def stage_probe_source(ex_id: str, scripts: dict) -> Path:
    """Stage a readiness-probe run source: only the probe scripts —
    staged under src/ like a project so the same fetch/unpack/exec
    factory runs them under the target's account. Never installs."""
    import tempfile
    staging_dir().mkdir(parents=True, exist_ok=True)
    dest = staging_dir() / f"{ex_id}.tar"
    tmpdir = Path(tempfile.mkdtemp(prefix="bb-probe-")) / "src"
    tmpdir.mkdir(parents=True)
    for name, content in scripts.items():
        (tmpdir / name).write_text(content, encoding="utf-8")
    tmp = dest.with_suffix(".tar.tmp")
    with tarfile.open(tmp, "w") as tar:
        tar.add(tmpdir, arcname="src")
    tmp.replace(dest)
    return dest


def ssh_exec_spec(ex, python: str, dispatcher_script: str,
                  spec_file: str) -> dict:
    return {"python": python, "dispatcher_script": dispatcher_script,
            "spec_file": spec_file}


def write_ssh_run_spec(ex, runner: Runner, runtime, args: list,
                       credential_file: str = "",
                       extra: dict = None,
                       executable: str = None,
                       source_tar: str = "",
                       execution_id: str = None) -> Path:
    """Per-run dispatcher spec (0600): connection + validated runner
    argv + remote paths + the staged source archive. Key material is
    referenced by the materialized key FILE path only. ``extra``
    carries node-runner fields (exec_subdir/env/report_rel); the
    keyword overrides let the readiness probe reuse this writer."""
    eid = execution_id or ex.id
    run_specs_dir().mkdir(parents=True, exist_ok=True)
    spec = {
        "execution_id": eid,
        "host": runner.ssh_host,
        "port": runner.ssh_port or 22,
        "username": runner.ssh_username,
        "key_file": str(run_specs_dir() / f"key-{runner.id}"),
        "host_key_fingerprint": runner.ssh_host_key_fingerprint,
        "host_key_alg": runner.ssh_host_key_alg,
        "remote_workdir": (runner.ssh_workdir.rstrip("/")
                           + "/" + eid),
        "source_tar": source_tar or str(staging_dir() / f"{eid}.tar"),
        "executable": executable or runtime.executable_path,
        "args": list(args),
        "credential_file": credential_file,
        "report_rel": "Reports",
        "timeout_s": int((ex.snapshot().get("run_config") or {})
                         .get("timeout_seconds") or 3600),
    }
    spec.update(extra or {})
    path = run_specs_dir() / f"{eid}.json"
    _write_json(path, spec)
    return path


def materialize_ssh_key(runner: Runner) -> str:
    """Write the stored SSH private key to a 0600 file the dispatcher
    reads — key bytes live only in the secrets store and this file."""
    from .secrets_store import read_secret as _read
    if not runner.ssh_credential_ref:
        return ""
    value = _read(current_app._get_current_object(),
                  runner.ssh_credential_ref)
    run_specs_dir().mkdir(parents=True, exist_ok=True)
    path = run_specs_dir() / f"key-{runner.id}"
    path.write_text(value)
    os.chmod(path, 0o600)
    return str(path)
