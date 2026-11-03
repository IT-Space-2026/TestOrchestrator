#!/usr/bin/env python3
"""Assemble the offline release bundle (§13) — explicit inventory.

Produces testorchestrator-<ver>-<platform>.tar.gz from an INCLUSION
policy (never "archive the working directory"): every included path is
declared and classified, required files are a hard gate, and the build
emits a packaged-file inventory + dependency manifest + sha256.

Exclusions from the runtime distribution (kept in the source repo):
- Java runtime: operator prerequisite — validated externally, never
  bundled (manifest/licences retained as the pinned reference).
- Acquisition archives (3rdparty/*/debs): extracted rootfs is already
  packaged; installers do not re-consume the .debs.
- tests/, caches (__pycache__, .pytest_cache), instance data, venvs,
  dist/, and any other build-only content.

KRE stays external by requirement. Bundled Python/Git/Caddy/Dagu/Allure
are the agreed offline payload — do NOT remove them.
"""
import hashlib
import json
import platform
import subprocess
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "dist"

# (repo path, category) — the complete inclusion policy.
INCLUDE = [
    ("testorchestrator", "app"),
    ("wsgi.py", "app"),
    ("requirements.txt", "app"),
    ("release", "app"),
    ("migrations", "install"),
    ("deploy", "install"),
    ("scripts", "install"),
    ("docs", "docs"),
    ("README.md", "docs"),
    ("CHANGELOG.md", "docs"),
    ("assets", "app"),
    ("3rdparty/python-runtime", "python-runtime"),
    ("3rdparty/python-wheels", "python-deps"),
    ("3rdparty/git", "git"),
    ("3rdparty/caddy", "caddy"),
    ("3rdparty/dagu", "dagu"),
    ("3rdparty/allure", "allure"),
    ("3rdparty/java-runtime", "java-ref"),   # manifest+licences only
    ("3rdparty/licenses", "docs"),
]

# Hard gate: the bundle is invalid without every one of these.
REQUIRED = [
    "wsgi.py", "testorchestrator/__init__.py", "requirements.txt",
    "release/release.json", "scripts/install.py",
    "scripts/java_probe.py", "migrations", "deploy/Caddyfile",
    "3rdparty/caddy/caddy",
    "3rdparty/python-runtime/python/bin/python3",
    "3rdparty/dagu",
    "3rdparty/allure",
]

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "instance", "dist",
                ".venv", "debs"}


def _excluded(rel: Path) -> bool:
    parts = set(rel.parts)
    if parts & EXCLUDE_DIRS:
        return True
    if any(p.endswith(".pyc") or p.endswith(".egg-info")
           for p in parts):
        return True
    # The JRE itself is never packaged — keep its manifests/licences.
    if "java-runtime" in parts and ({"rootfs", "debs"} & parts):
        return True
    return False


def collect() -> tuple[list[tuple[Path, str, str]], list[str]]:
    """(abs path, arc rel, category) for everything the policy includes,
    plus the list of missing REQUIRED entries."""
    files, missing = [], []
    for item, category in INCLUDE:
        src = REPO / item
        if not src.exists():
            if item in REQUIRED:
                missing.append(item)
            continue
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                if p.is_dir() and not p.is_symlink():
                    continue
                rel = p.relative_to(REPO)
                if not _excluded(rel):
                    files.append((p, str(rel), category))
        else:
            rel = src.relative_to(REPO)
            if not _excluded(rel):
                files.append((src, str(rel), category))
    for req in REQUIRED:
        if not (REPO / req).exists():
            missing.append(req)
    return files, missing


DEPENDENCIES = {
    "java-runtime": {
        "required_for": "Allure report generation (optional)",
        "requirement": "compatible JRE/JDK — OpenJDK 21+ recommended; "
                       "validated by an actual report-generation probe "
                       "at install, re-checkable in Admin > Reporting",
        "install_flags": "--java-home <path> | --no-allure",
    },
    "kre": {
        "required_for": "test execution",
        "requirement": "operator-installed, licensed Katalon Runtime "
                       "Engine — never installed or altered by the "
                       "product",
    },
    "reportportal": {
        "required_for": "optional remote publication",
        "requirement": "operator-provided endpoint + credentials",
    },
    "git": {
        "required_for": "none on ubuntu targets — vendored; other "
                        "platforms fall back to declared host git",
    },
}


def main():
    meta = json.loads((REPO / "release" / "release.json").read_text())
    plat = f"{platform.system().lower()}-{platform.machine()}"
    name = f"testorchestrator-{meta['version']}-{plat}"
    OUT.mkdir(exist_ok=True)
    bundle = OUT / f"{name}.tar.gz"

    files, missing = collect()
    if missing:
        raise SystemExit(f"bundle build FAILED — required files missing: "
                         f"{missing}")
    t0 = time.time()
    with tarfile.open(bundle, "w:gz") as tar:
        for src, rel, _cat in files:
            tar.add(src, arcname=f"{name}/{rel}", recursive=False)
    build_s = round(time.time() - t0, 1)

    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (OUT / f"{name}.sha256").write_text(f"{sha}  {bundle.name}\n")

    categories = {}
    for src, rel, cat in files:
        c = categories.setdefault(cat, {"files": 0, "bytes": 0})
        c["files"] += 1
        c["bytes"] += src.lstat().st_size
    extracted = sum(c["bytes"] for c in categories.values())

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO,
        capture_output=True, text=True).stdout.strip()
    (OUT / f"{name}.inventory.json").write_text(json.dumps({
        "name": name, "files": [
            {"path": rel, "category": cat, "size": src.lstat().st_size}
            for src, rel, cat in files],
    }, indent=0))
    (OUT / f"{name}.manifest.json").write_text(json.dumps({
        "name": name, "version": meta["version"], "platform": plat,
        "file": bundle.name,
        "size_bytes": bundle.stat().st_size,
        "extracted_bytes": extracted,
        "sha256": sha,
        "source_commit": commit,
        "build_seconds": build_s,
        "categories": categories,
        "includes": [i for i, _c in INCLUDE],
        "bundled": {
            "python-runtime": "python-build-standalone 3.12 linux x86_64",
            "python-wheels": "vendored, pip --no-index install",
            "git": "vendored ubuntu-resolute-amd64 rootfs",
            "caddy": "pinned binary + licence + sha256",
            "dagu": "pinned 2.25.2 release tarball",
            "allure": "pinned allure-commandline generator",
        },
        "dependencies": DEPENDENCIES,
        "exclusions": {
            "java-runtime": "operator prerequisite — never bundled; "
                            "validated by generation probe at install",
            "acquisition-debs": "3rdparty/*/debs — extracted rootfs "
                                "already packaged",
            "tests": "source-repo content, not runtime",
            "kre": "external by requirement — operator installed",
            "reportportal-stack": "container backend + images not bundled",
            "windows-sles": "linux x86_64 only",
        }}, indent=2))

    print(f"{bundle} ({bundle.stat().st_size / 1e6:.1f} MB, "
          f"extracted {extracted / 1e6:.0f} MB, built {build_s}s)")
    for cat, c in sorted(categories.items(),
                         key=lambda kv: -kv[1]["bytes"]):
        print(f"  {cat:<16} {c['bytes'] / 1e6:>7.1f} MB "
              f"({c['files']} files)")


if __name__ == "__main__":
    main()

# Dagu is shipped as its release tarball under 3rdparty/dagu/ — extract
# during bundle assembly/install, not committed uncompressed (>100MB).
