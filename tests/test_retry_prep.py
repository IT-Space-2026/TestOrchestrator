"""Retry-prep script regression tests — the REAL `sh -c` command the
master renders, exercised against live processes.

Coverage of the PB-loss retry boundaries:
- env-marker match → SIGTERM, termination CONFIRMED (procfs gone)
  before the script exits 0 and the build proceeds;
- env-marker mismatch or unreadable → exit 70 + TO_RETRY_BLOCKED,
  the foreign process left untouched — no relaunch while the
  original attempt's liveness is unresolved;
- attempt counter → explicit max_attempts cap and retry_enabled
  policy, enforced per-execution in the builddir.
"""

import ast
import json
import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

MASTER_CFG = (Path(__file__).resolve().parent.parent
              / "deploy" / "buildbot" / "master.cfg")


def _load_prep_argv():
    """Compile just _prep_argv out of master.cfg (the file's module
    level needs the buildbot package, which tests do not install)."""
    tree = ast.parse(MASTER_CFG.read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "_prep_argv")
    fn.decorator_list = []          # @util.renderer is buildbot-side
    ns = {"json": json, "os": os}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),
                 str(MASTER_CFG), "exec"), ns)
    return ns["_prep_argv"]


class _Props:
    def __init__(self, vals):
        self._v = vals

    def getProperty(self, name):
        return self._v.get(name)


def _props(eid, max_attempts=3, retry_enabled=True):
    return _Props({
        "to_execution_id": eid,
        "exec_spec": json.dumps({
            "max_attempts": max_attempts,
            "retry_enabled": retry_enabled}),
    })


def _run_prep(tmp_path, eid, **spec):
    argv = _load_prep_argv()(_props(eid, **spec))
    return subprocess.run(argv, cwd=tmp_path, capture_output=True,
                          text=True, timeout=30)


def _spawn(eid_marker=None):
    """A detached stand-in for an orphaned test CLI. Double-forked via
    an intermediate sh so the process is reparented to init — the
    production condition (worker dead → orphan reaped by init on
    kill). A still-parented victim would zombie instead, which is not
    the orphan shape prep resolves."""
    env = dict(os.environ)
    if eid_marker:
        env["TO_EXEC_ID"] = eid_marker
    p = subprocess.Popen(["sh", "-c", "sleep 300 & echo $!"],
                         env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    # readline, not read: the orphan inherits the write end, so the
    # pipe never reaches EOF — only the pid line is wanted.
    pid = int(p.stdout.readline().strip())
    p.wait()                            # intermediate dead → orphan
    p.stdout.close()
    return pid


def _alive(pid):
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}").exists()
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not Path("/proc/self/environ").exists(),
    reason="needs Linux /proc environ inspection")


def test_matching_orphan_terminated_and_confirmed(tmp_path):
    eid = "execA"
    pid = _spawn(eid_marker=eid)
    (tmp_path / ".to_exec_pid").write_text(str(pid))
    r = _run_prep(tmp_path, eid)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "terminated orphaned prior attempt" in r.stdout
    assert "confirmed dead" in r.stdout
    assert not _alive(pid)            # confirmed inside the script
    assert not (tmp_path / ".to_exec_pid").exists()
    # attempt ledger written for this execution
    assert (tmp_path / f".to_exec_attempt_{eid}").read_text().strip() == "1"


def test_foreign_pid_blocks_relaunch_and_survives(tmp_path):
    """A live pid whose environ carries NO TO_EXEC_ID for this run —
    marker mismatch (or unreadable environ) blocks the launch; the
    process is left running for an operator, never killed blind."""
    eid = "execB"
    pid = _spawn()                     # no env marker at all
    try:
        (tmp_path / ".to_exec_pid").write_text(str(pid))
        r = _run_prep(tmp_path, eid)
        assert r.returncode == 70
        assert "TO_RETRY_BLOCKED" in r.stdout
        assert _alive(pid)
        # pidfile retained as investigation evidence
        assert (tmp_path / ".to_exec_pid").exists()
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_max_attempts_cap_blocks_extra_relaunch(tmp_path):
    eid = "execC"
    (tmp_path / f".to_exec_attempt_{eid}").write_text("3")
    r = _run_prep(tmp_path, eid, max_attempts=3)
    assert r.returncode == 70
    assert "exceeds max_attempts" in r.stdout


def test_retry_disabled_blocks_any_retry(tmp_path):
    eid = "execD"
    (tmp_path / f".to_exec_attempt_{eid}").write_text("1")
    r = _run_prep(tmp_path, eid, retry_enabled=False)
    assert r.returncode == 70
    assert "retries disabled" in r.stdout


def test_first_attempt_proceeds_and_counts(tmp_path):
    eid = "execE"
    r = _run_prep(tmp_path, eid)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / f".to_exec_attempt_{eid}").read_text().strip() == "1"
    # a SECOND prep entry (same builddir, resubmitted brid) counts up
    r2 = _run_prep(tmp_path, eid)
    assert r2.returncode == 0
    assert "attempt 2 of" in r2.stdout
    assert (tmp_path / f".to_exec_attempt_{eid}").read_text().strip() == "2"


def test_attempt_counters_are_per_execution(tmp_path):
    """A stale counter from a DIFFERENT execution in the same
    builddir must not count against this run — and is cleaned."""
    eid = "execF"
    (tmp_path / ".to_exec_attempt_other").write_text("9")
    r = _run_prep(tmp_path, eid)
    assert r.returncode == 0
    assert (tmp_path / f".to_exec_attempt_{eid}").read_text().strip() == "1"
    assert not (tmp_path / ".to_exec_attempt_other").exists()


def test_dead_pidfile_reports_exited_and_proceeds(tmp_path):
    eid = "execG"
    (tmp_path / ".to_exec_pid").write_text("999999")
    r = _run_prep(tmp_path, eid)
    assert r.returncode == 0
    assert "already exited" in r.stdout
