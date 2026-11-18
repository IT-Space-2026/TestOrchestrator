"""Assembled-package gate: offline deps + vendored reporting-labs +
worker assets on the extracted artifact."""
import hashlib, re, subprocess, sys, tempfile
from pathlib import Path


def _worker_assets(pkg: Path) -> None:
    """The package must carry the downloadable worker assets:
    - the Buildbot-worker NSIS installer (Windows) under
      packaging/windows, version-aligned with the pinned stack
    - the controller-side buildbot wheels dir (GPL-approved builds)."""
    exes = sorted((pkg / "packaging" / "windows").glob(
        "TestOrchestrator-Buildbot-Worker-Setup-*.exe"))
    assert exes, "buildbot-worker Windows installer missing from package"
    assert len(exes) == 1, f"multiple worker installers: {[e.name for e in exes]}"
    m = re.search(r"Setup-(\d+\.\d+\.\d+)", exes[0].name)
    assert m, f"unrecognised installer name {exes[0].name}"
    wheels = pkg / "3rdparty" / "buildbot-wheels"
    assert wheels.is_dir(), "buildbot controller wheels missing"
    names = {w.name for w in wheels.glob("*.whl")}
    assert any(n.startswith(f"buildbot-{m.group(1)}") for n in names), \
        f"no buildbot=={m.group(1)} wheel in bundle"
    assert any(n.startswith("buildbot_worker") for n in names), \
        "buildbot-worker wheel missing from bundle"
    assert any(n.startswith("paramiko") for n in names), \
        "paramiko wheel missing (SSH dispatcher dep)"


def main():
    pkg=Path(sys.argv[1]);py=pkg/"3rdparty/python-runtime/python/bin/python3"
    with tempfile.TemporaryDirectory() as td:
        v=Path(td)/"v"
        subprocess.run([str(py),"-m","venv",str(v)],check=True)
        subprocess.run([str(v/"bin/pip"),"install","--no-index","--no-cache-dir",
            "--find-links",str(pkg/"3rdparty/python-wheels"),
            "-r",str(pkg/"requirements.txt")],check=True)
        subprocess.run([str(v/"bin/pip"),"check"],check=True)
        # reporting-labs is vendored at 3rdparty/reporting_labs — resolve
        # it exactly as the running app does: import testorchestrator so
        # the vendored-source bootstrap installs, then import the module
        # and pin its file location under 3rdparty/.
        r=subprocess.run([str(v/"bin/python"),"-c",
            "import testorchestrator,reporting_labs,os;"
            "assert 'site-packages' not in reporting_labs.__file__, reporting_labs.__file__;"
            "assert os.sep+'3rdparty'+os.sep in reporting_labs.__file__, reporting_labs.__file__;"
            "print(reporting_labs.__version__)"],
            capture_output=True,text=True,cwd=pkg)
        assert r.returncode==0,f"reporting_labs import failed: {r.stderr}"
        assert r.stdout.strip()=="0.1.14",r.stdout
        _worker_assets(pkg)
        print("GATE OK")
if __name__=="__main__":
    main()
