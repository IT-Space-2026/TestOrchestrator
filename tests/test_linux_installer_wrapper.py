"""Self-extracting .run wrapper — fixture-payload behavior."""
import os, subprocess, tarfile
from pathlib import Path
from scripts.build_linux_installer import build


def _fixture_payload(workdir, install_exit=0, marker="ok"):
    install = workdir / "payload" / "install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        '#!/bin/sh\n'
        'echo "ARGS:$*"\necho "CWD:$(pwd)"\n'
        'echo "' + marker + '"\nexit ' + str(install_exit) + '\n')
    install.chmod(0o755)
    tb = workdir / "payload.tar.gz"
    with tarfile.open(tb, "w:gz") as t:
        t.add(install.parent, arcname=".")
    return tb


def test_spaces_and_args(tmp_path):
    tb = _fixture_payload(tmp_path, marker="PAYLOAD-OK")
    run = tmp_path / "with space" / "to.run"
    run.parent.mkdir(parents=True)
    build(tb, run); os.chmod(run, 0o755)
    r = subprocess.run([str(run), "--payload", "/opt/x y"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "PAYLOAD-OK" in r.stdout
    assert '--payload /opt/x y' in r.stdout


def test_nonzero_propagates(tmp_path):
    tb = _fixture_payload(tmp_path, install_exit=3)
    run = tmp_path / "to.run"
    build(tb, run); os.chmod(run, 0o755)
    r = subprocess.run([str(run)], capture_output=True, text=True)
    assert r.returncode == 3


def test_cleanup_scope(tmp_path):
    tb = _fixture_payload(tmp_path)
    run = tmp_path / "to.run"
    build(tb, run); os.chmod(run, 0o755)
    before = set(Path("/tmp").glob("to-install-*"))
    subprocess.run([str(run)], capture_output=True, text=True)
    assert before == set(Path("/tmp").glob("to-install-*"))
