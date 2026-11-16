"""Assembled-package gate: offline deps + vendored reporting-labs on the
extracted artifact."""
import hashlib, re, subprocess, sys, tempfile
from pathlib import Path


def _worker_assets(pkg: Path) -> None:
    """The package must carry the downloadable worker assets:
    - the NSIS dagu-worker installer (Windows) under packaging/windows
    - both pinned dagu archives under 3rdparty/dagu, checksum-verified
      and version-aligned with the installer."""
    dagu = pkg / "3rdparty" / "dagu"
    sums = {}
    for line in (dagu / "dagu.sha256").read_text().splitlines():
        h, name = line.split()
        sums[name] = h
    versions = set()
    for name, expected in sums.items():
        p = dagu / name
        assert p.is_file(), f"missing pinned archive {name}"
        m = re.fullmatch(r"dagu_(\d+\.\d+\.\d+)_\w+_amd64\.tar\.gz", name)
        assert m, f"unrecognised archive name {name}"
        versions.add(m.group(1))
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        assert h.hexdigest() == expected, f"checksum mismatch {name}"
    assert len(versions) == 1, f"coordinator/worker version skew {versions}"
    ver = versions.pop()
    exes = sorted((pkg / "packaging" / "windows").glob(
        "testorchestrator-dagu-worker-*-setup-UNSIGNED.exe"))
    assert exes, "dagu-worker Windows installer missing from package"
    assert len(exes) == 1, f"multiple worker installers: {[e.name for e in exes]}"
    assert f"-{ver}-" in exes[0].name or exes[0].name.endswith(
        f"-{ver}-setup-UNSIGNED.exe"), \
        f"worker installer {exes[0].name} not aligned to dagu {ver}"


def main():
    pkg=Path(sys.argv[1]);py=pkg/"3rdparty/python-runtime/python/bin/python3"
    with tempfile.TemporaryDirectory() as td:
        v=Path(td)/"v"
        subprocess.run([str(py),"-m","venv",str(v)],check=True)
        subprocess.run([str(v/"bin/pip"),"install","--no-index","--no-cache-dir",
            "--find-links",str(pkg/"3rdparty/python-wheels"),
            "-r",str(pkg/"requirements.txt")],check=True)
        subprocess.run([str(v/"bin/pip"),"check"],check=True)
        # reporting-labs is vendored at the package root (./reporting_labs) —
        # import it with cwd=pkg exactly as the running app does.
        r=subprocess.run([str(v/"bin/python"),"-c",
            "import reporting_labs,os;"
            "assert 'site-packages' not in reporting_labs.__file__, reporting_labs.__file__;"
            "print(reporting_labs.__version__)"],
            capture_output=True,text=True,cwd=pkg)
        assert r.returncode==0,f"reporting_labs import failed: {r.stderr}"
        assert r.stdout.strip()=="0.1.14",r.stdout
        _worker_assets(pkg)
        print("GATE OK")
if __name__=="__main__":
    main()
