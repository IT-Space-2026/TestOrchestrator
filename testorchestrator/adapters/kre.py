"""KRE (Katalon Runtime Engine) adapter — connect-only (handoff §5).

KRE is installed, licensed, updated, and removed externally by the operator.
This adapter registers existing installations and invokes them; it NEVER
installs, downloads, imports, upgrades, or licenses KRE. Executable
existence alone is not a claim of licence readiness.

All invocations use subprocess argument arrays over allowlisted inputs —
never concatenated shell text. Workspaces are isolated per attempt at the
recorded source SHA with a dedicated report directory. Cancellation targets
the actual process tree.

NOTE: argument spelling is version-aware (verified KRE CLI schema pending
operator-supplied executable; see docs/integration-contracts.md). The
fixture executable used in tests proves the pipeline mechanics — it does
not prove KRE CLI compatibility (A14).
"""

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class KreError(RuntimeError):
    """Actionable external-prerequisite failure (missing/incompatible KRE)."""


@dataclass
class InvocationResult:
    started_at: str = ""
    finished_at: str = ""
    exit_status: int | None = None
    timed_out: bool = False
    canceled: bool = False
    stdout_path: str = ""
    stderr_path: str = ""
    pid: int = 0
    error_kind: str = ""   # license|config|infrastructure|execution
    error_text: str = ""


# Well-known KRE console flag set (Katalon-documented form). Kept version-
# aware: flags may be overridden per detected version once the operator's
# installed version is probed — never assumed universal (§5).
def build_args(
    executable: str,
    project_path: str,
    suite_path: str,
    report_dir: str,
    profile: str = "default",
    browser: str = "Chrome (headless)",
    extra: list | None = None,
) -> list:
    return [
        executable,
        "-noSplash",
        "-runMode=console",
        f"-projectPath={project_path}",
        f"-testSuitePath={suite_path}",
        f"-executionProfile={profile}",
        f"-browserType={browser}",
        f"-reportFolder={report_dir}",
        *(extra or []),
    ]


def probe_version(executable: str, timeout: int = 30) -> str:
    """Probe an existing installation's version. Raises KreError when the
    executable is missing or unprobeable — actionable prerequisite error."""

    path = Path(executable)
    if not path.is_absolute():
        raise KreError(f"executable path must be absolute: {executable}")
    if not path.exists():
        raise KreError(f"KRE executable not found: {executable}")
    if not os.access(path, os.X_OK):
        raise KreError(f"KRE path is not executable: {executable}")
    try:
        proc = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KreError(f"version probe failed for {executable}: {exc}")
    text = (proc.stdout + proc.stderr).strip()
    match = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    return match.group(1) if match else text[:64] or "unknown"


def prepare_workspace(source_dir: Path, dest: Path, sha: str) -> Path:
    """Isolated checkout at the recorded source SHA (§5). For a local runner
    the worktree copy is made under the runner workspace; source contents
    are copied verbatim — project code is never executed during indexing."""

    workspace = dest / "project"
    if source_dir.exists():
        shutil.copytree(source_dir, workspace)
    else:
        workspace.mkdir(parents=True)
    (dest / "snapshot.sha").write_text(sha, encoding="utf-8")
    return workspace


def run_invocation(
    args: list,
    log_dir: Path,
    timeout_s: int = 3600,
    cancel_event=None,
    env_extra: dict | None = None,
) -> InvocationResult:
    """Execute an invocation and capture evidence (§5).

    - argv array only; no shell
    - own process group so cancel kills the tree
    - stdout/stderr captured to files (never logged to app logs verbatim
      if they could carry secrets — artifact collection handles policy)
    - cancel stays pending-aware: process termination is awaited
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    result = InvocationResult()
    stdout_f = open(log_dir / "kre-stdout.log", "wb")
    stderr_f = open(log_dir / "kre-stderr.log", "wb")
    result.stdout_path = str(stdout_f.name)
    result.stderr_path = str(stderr_f.name)
    result.started_at = datetime.now(timezone.utc).isoformat()

    env = dict(os.environ)
    env.update(env_extra or {})

    try:
        proc = subprocess.Popen(
            args, stdout=stdout_f, stderr=stderr_f, env=env,
            start_new_session=True,  # own process group for tree cancel
        )
    except OSError as exc:
        stdout_f.close(); stderr_f.close()
        raise KreError(f"failed to launch {args[0]}: {exc}")

    result.pid = proc.pid
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            _kill_tree(proc)
            result.canceled = True
            break
        if time.monotonic() > deadline:
            _kill_tree(proc)
            result.timed_out = True
            break
        time.sleep(0.25)

    try:
        result.exit_status = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, force=True)
        result.exit_status = proc.wait()

    result.finished_at = datetime.now(timezone.utc).isoformat()
    stdout_f.close(); stderr_f.close()
    return result


def _kill_tree(proc, force: bool = False) -> None:
    """Terminate the real process tree (process group), escalating TERM->KILL."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def classify_error(result: InvocationResult, stdout_tail: str = "") -> str:
    """Distinguish licence/configuration/infrastructure errors from test
    failures (§5). Missing results never masquerade as a passed run."""
    if result.canceled:
        return ""
    if result.timed_out:
        return "infrastructure"
    text = stdout_tail.lower()
    if any(s in text for s in ("license", "licence", "activation")):
        return "license"
    if any(s in text for s in ("projectpath", "no such file", "cannot find project")):
        return "config"
    if result.exit_status not in (0, 1):
        return "infrastructure"
    return ""


def discover_reports(report_dir: Path) -> list:
    """Report discovery: JUnit XML outputs produced by the invocation."""
    if not report_dir.exists():
        return []
    return sorted(
        p for p in report_dir.rglob("*.xml")
        if p.is_file() and not p.name.startswith(".")
    )
