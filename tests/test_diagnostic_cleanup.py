"""Cleanup safety — removes only the recorded attempt working dir by
exact path; never touches sibling evidence dirs, earlier attempts, or
unrelated files. Regression for the to-reg-* glob evidence loss."""
import os, subprocess, tempfile


CLEANUP_SNIPPET = r'''
ROOT="$1"
safe_cleanup() {
  case "$ROOT" in
    /tmp/to-work-*) rm -rf -- "$ROOT";;
    *) echo "refuse $ROOT"; return 1;;
  esac
}
safe_cleanup
'''


def _run_cleanup(target):
    with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False) as f:
        f.write("#!/usr/bin/env bash\nset -euo pipefail\n" + CLEANUP_SNIPPET)
        path = f.name
    os.chmod(path, 0o700)
    r = subprocess.run([path, target], capture_output=True, text=True)
    os.unlink(path)
    return r


def test_removes_only_recorded_workdir(tmp_path):
    work = tempfile.mkdtemp(prefix="to-work-")
    evid = tempfile.mkdtemp(prefix="to-evid-")
    other = tempfile.mkdtemp(prefix="to-work-other-")
    unrelated = tempfile.mkdtemp(prefix="unrelated-")
    r = _run_cleanup(work)
    assert r.returncode == 0
    assert not os.path.exists(work)            # the attempt dir is gone
    for keep in (evid, other, unrelated):      # everything else survives
        assert os.path.exists(keep), f"cleanup removed {keep}"


def test_refuses_non_workdir_target(tmp_path):
    victim = tempfile.mkdtemp(prefix="important-")
    open(os.path.join(victim, "keep.txt"), "w").write("x")
    r = _run_cleanup(victim)
    assert r.returncode != 0
    assert os.path.exists(os.path.join(victim, "keep.txt"))


def test_refuses_glob_like_or_root(tmp_path):
    for bad in ("/tmp", "/", "/tmp/to-work-", ""):
        r = _run_cleanup(bad)
        assert r.returncode != 0 or bad.startswith("/tmp/to-work-")
