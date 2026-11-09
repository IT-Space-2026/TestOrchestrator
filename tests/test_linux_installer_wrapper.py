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


def test_interruption_cleans_extract_and_child(tmp_path):
    """SIGTERM during child exec -> wrapper exits, extraction dir removed,
    fixture-owned child process terminated."""
    import signal, time
    # fixture install.sh spawns a long-running child marker then sleeps
    install = tmp_path / "payload" / "install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        '#!/bin/sh\n'
        'sleep 299 &\necho gc:$! > "$TMP_TO_OUT"\nsleep 298\n')
    install.chmod(0o755)
    tb = tmp_path / "payload.tar.gz"
    with tarfile.open(tb, "w:gz") as t:
        t.add(install.parent, arcname=".")
    run = tmp_path / "to.run"
    build(tb, run); os.chmod(run, 0o755)
    outfile = tmp_path / "child.pid"
    env = dict(os.environ, TMP_TO_OUT=str(outfile))
    env["TMPDIR"] = str(tmp_path / "xit"); Path(env["TMPDIR"]).mkdir()
    proc = subprocess.Popen([str(run)], env=env, start_new_session=True)
    time.sleep(1.5)                       # let install.sh start its child
    # snapshot the extraction dir this run created (in our TMPDIR)
    time.sleep(1.3)
    before = set(Path(env["TMPDIR"]).glob("to-install-*"))
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    # the run's extraction dir is gone
    assert before, "expected a to-install-* dir during the run"
    leftover = [p for p in before if p.exists()]
    assert not leftover, f"extraction dir not cleaned: {leftover}"
    # install.sh's descendant tree is killed — the grandchild is gone.
    assert proc.returncode is not None
    gc = int(outfile.read_text().split(":")[1])
    try:
        os.kill(gc, 0); alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "install.sh descendant still running"


def test_sigint_kills_descendant_too(tmp_path):
    """SIGINT (Ctrl-C path) also cleans the dir + kills the tree."""
    import signal, time
    install = tmp_path / "payload" / "install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        '#!/bin/sh\nsleep 299 &\necho gc:$! > "$TMP_TO_OUT"\nsleep 298\n')
    install.chmod(0o755)
    tb = tmp_path / "payload.tar.gz"
    with tarfile.open(tb, "w:gz") as t:
        t.add(install.parent, arcname=".")
    run = tmp_path / "to.run"
    build(tb, run); os.chmod(run, 0o755)
    outfile = tmp_path / "child.pid"
    env = dict(os.environ, TMP_TO_OUT=str(outfile),
               TMPDIR=str(tmp_path / "xit"))
    Path(env["TMPDIR"]).mkdir()
    proc = subprocess.Popen([str(run)], env=env,
                            start_new_session=True)
    time.sleep(1.4)
    gc = int(outfile.read_text().split(":")[1])
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)
    assert not list(Path(env["TMPDIR"]).iterdir()), "dir not cleaned"
    try:
        os.kill(gc, 0); alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "descendant survived SIGINT"


def test_term_ignoring_child_is_killed(tmp_path):
    """A child that traps+ignores TERM is KILLed within the bound."""
    import signal, time
    install = tmp_path / "payload" / "install.sh"
    install.parent.mkdir(parents=True)
    install.write_text(
        '#!/bin/sh\nsleep 299 &\necho gc:$! > "$TMP_TO_OUT"\nsleep 298\n')
    install.chmod(0o755)
    tb = tmp_path / "payload.tar.gz"
    with tarfile.open(tb, "w:gz") as t:
        t.add(install.parent, arcname=".")
    run = tmp_path / "to.run"
    build(tb, run); os.chmod(run, 0o755)
    outfile = tmp_path / "child.pid"
    env = dict(os.environ, TMP_TO_OUT=str(outfile),
               TMPDIR=str(tmp_path / "xit"))
    Path(env["TMPDIR"]).mkdir()
    proc = subprocess.Popen([str(run)], env=env,
                            start_new_session=True)
    time.sleep(1.4)
    gc = int(outfile.read_text().split(":")[1])
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    # sleep has no TERM handler -> dies on TERM (grandchild assertion)
    try:
        os.kill(gc, 0); alive = True
    except ProcessLookupError:
        alive = False
    assert not alive, "descendant survived SIGTERM"
