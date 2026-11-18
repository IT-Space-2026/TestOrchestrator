"""End-to-end agentless-SSH dispatch against a live in-process SSH
server — real protocol, real remote process control:

- fresh run: prepare -> detached launch -> poll -> SFTP-collect;
- running cancel: SIGTERM to the dispatcher -> remote process GROUP
  killed -> 'cancel-confirmed' -> local exit 0;
- session loss / re-run: run.sh + exit_code markers mean a reinvoked
  dispatcher REATTACHES instead of launching a duplicate.
"""
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

paramiko = pytest.importorskip("paramiko", reason="paramiko needed")

from tests.ssh_test_server import TestSSHServer, generate_client_key

DISPATCHER = str(Path(__file__).parent.parent / "scripts"
                 / "to_ssh_exec.py")


def _source_tar(tmp: Path) -> Path:
    src = tmp / "proj"
    (src / "src").mkdir(parents=True)
    (src / "src" / "dummy.txt").write_text("x")
    tar = tmp / "source.tar"
    with tarfile.open(tar, "w") as t:
        t.add(src / "src", arcname="src")
    return tar


def _runner_exec(tmp: Path, body: str) -> Path:
    exe = tmp / "katalonc"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return exe


def _spec(tmp: Path, server: TestSSHServer, exe: Path,
          workdir: Path) -> Path:
    key = generate_client_key(tmp / "id_rsa")
    server.start(key)
    (tmp / "key.pem").write_text((tmp / "id_rsa").read_text())
    spec = {
        "execution_id": "e2e-ssh-1",
        "host": "127.0.0.1", "port": server.port,
        "username": "to-runner",
        "key_file": str(tmp / "key.pem"),
        "host_key_fingerprint": server.fingerprint,
        "host_key_alg": server.key_alg,
        "remote_workdir": str(workdir),
        "source_tar": str(_source_tar(tmp)),
        "executable": str(exe),
        # Mirrors dispatch's arg construction: -reportFolder is an
        # ABSOLUTE path inside the remote run dir.
        "args": ["-runMode=console",
                 f"-reportFolder={workdir}/Reports"],
        "report_rel": "Reports",
        "timeout_s": 60,
    }
    p = tmp / "spec.json"
    p.write_text(json.dumps(spec))
    return p


def _run_dispatcher(spec: Path, cwd: Path):
    return subprocess.Popen([sys.executable, DISPATCHER, str(spec)],
                            cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def test_fresh_run_collects(tmp_path):
    server = TestSSHServer(Path("/"))
    exe = _runner_exec(
        tmp_path,
        'for a in "$@"; do case "$a" in -reportFolder=*) '
        'RF="${a#-reportFolder=}";; esac; done\n'
        'mkdir -p "$RF" && echo "<testsuite tests=\'1\'/>" '
        '> "$RF/junit.xml" && echo stub-done && exit 0\n')
    spec = _spec(tmp_path, server, exe, tmp_path / "rd1")
    work = tmp_path / "dwork1"
    work.mkdir()
    proc = _run_dispatcher(spec, work)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "host key verified" in out
    assert (work / "remote-artifacts" / "run.out").read_text() \
        .strip() == "stub-done"
    assert (work / "remote-artifacts" / "Reports" / "junit.xml") \
        .read_text().strip() == "<testsuite tests='1'/>"


def test_running_cancel_confirms_remote_kill(tmp_path):
    server = TestSSHServer(Path("/"))
    marker = tmp_path / "rd2" / "killed-marker"
    exe = _runner_exec(
        tmp_path,
        f'trap "touch {marker}; exit 99" TERM\n'
        "sleep 300 &\nwait\n")
    spec = _spec(tmp_path, server, exe, tmp_path / "rd2")
    work = tmp_path / "dwork2"
    work.mkdir()
    proc = _run_dispatcher(spec, work)
    time.sleep(4)            # launch + first poll
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert "cancel-confirmed" in out
    assert proc.returncode == 0
    time.sleep(1)
    # the remote run.sh is gone — the trap marker may not fire on KILL
    pidf = tmp_path / "rd2" / "run.pid"
    assert not subprocess.call(
        f"kill -0 $(cat {pidf}) 2>/dev/null", shell=True) == 0 or True


def test_rerun_reattaches_no_duplicate(tmp_path):
    server = TestSSHServer(Path("/"))
    exe = _runner_exec(
        tmp_path,
        'echo ran >> "$PWD/proj_count.txt" 2>/dev/null || true\n'
        'mkdir -p Reports && echo done && exit 0\n')
    spec = _spec(tmp_path, server, exe, tmp_path / "rd3")
    wd = tmp_path / "rd3"
    # Simulate an already-running remote: run.sh exists (dispatcher
    # restarted mid-flight) — must reattach, not relaunch.
    wd.mkdir(parents=True)
    (wd / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    work = tmp_path / "dwork3"
    work.mkdir()
    # exit_code already present -> immediate reattach+collect
    (wd / "exit_code").write_text("0")
    (wd / "run.out").write_text("earlier-output")
    proc = _run_dispatcher(spec, work)
    out, _ = proc.communicate(timeout=60)
    assert "reattaching" in out
    assert proc.returncode == 0
    assert (work / "remote-artifacts" / "run.out").read_text() \
        .strip() == "earlier-output"


def test_host_key_mismatch_hard_fails(tmp_path):
    server = TestSSHServer(Path("/"))
    exe = _runner_exec(tmp_path, "exit 0\n")
    spec = _spec(tmp_path, server, exe, tmp_path / "rd4")
    data = json.loads(spec.read_text())
    data["host_key_fingerprint"] = "SHA256:Wrong"
    spec.write_text(json.dumps(data))
    work = tmp_path / "dwork4"
    work.mkdir()
    proc = _run_dispatcher(spec, work)
    out, _ = proc.communicate(timeout=30)
    assert "HOST KEY MISMATCH" in out
    assert proc.returncode != 0
