#!/usr/bin/env python3
"""Assemble the offline release bundle (§13).

Produces testorchestrator-<ver>-<platform>.tar.gz containing the app, vendored
wheels, bundled Caddy, deploy material, and install.py — no registry/CDN
access needed at install time. Git is a HOST PREREQUISITE (GitPython shells
out to git CLI); a Python 3.12 interpreter is required on the host (embedded
runtime is listed as a gap, see packaging status).
"""
import json, platform, subprocess, sys, tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "dist"

INCLUDE = ["testorchestrator", "wsgi.py", "requirements.txt", "release",
           "3rdparty", "migrations", "deploy", "scripts", "docs",
           "README.md", "CHANGELOG.md", "assets", "tests"]

def main():
    meta = json.loads((REPO / "release" / "release.json").read_text())
    plat = f"{platform.system().lower()}-{platform.machine()}"
    name = f"testorchestrator-{meta['version']}-{plat}"
    OUT.mkdir(exist_ok=True)
    bundle = OUT / f"{name}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        for item in INCLUDE:
            p = REPO / item
            if p.exists():
                tar.add(p, arcname=f"{name}/{item}",
                        filter=lambda i: None if "__pycache__" in i.name else i)
    import hashlib
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO,
        capture_output=True, text=True).stdout.strip()
    (OUT / f"{name}.manifest.json").write_text(json.dumps({
        "name": name, "version": meta["version"], "platform": plat,
        "file": bundle.name,
        "size_bytes": bundle.stat().st_size,
        "sha256": sha,
        "source_commit": commit,
        "includes": INCLUDE,
        "exclusions": {
            "embedded-python": "host python3.12 required (gap)",
            "git-binary": "host git required by GitPython (gap)",
            "reportportal-stack": "container backend + images not bundled (gap)",
            "embedded-python": "bundled linux x86_64 only",
            "kre": "external by requirement — operator installed",
        }}, indent=2))
    print(bundle)

if __name__ == "__main__":
    main()

# Dagu is shipped as its release tarball under 3rdparty/dagu/ — extract
# during bundle assembly/install, not committed uncompressed (>100MB).
