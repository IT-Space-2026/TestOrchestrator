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

KRE stays external by requirement. Bundled Python/Caddy/Dagu/Allure are
the agreed offline payload. Bundled git remains only in the standard
build (opt-in cli backend); the frozen candidate is Dulwich-only.
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
    ("install.sh", "install"),
    ("requirements.txt", "app"),
    ("release", "app"),
    ("migrations", "install"),
    ("deploy", "install"),
    ("scripts", "install"),
    ("packaging", "install"),
    ("docs", "docs"),
    ("README.md", "docs"),
    ("CHANGELOG.md", "docs"),
    ("assets", "app"),
    ("3rdparty/python-runtime", "python-runtime"),
    ("3rdparty/python-wheels", "python-deps"),
    # --frozen candidate swaps the two entries above for the onedir.
    # Bundled git removed: all workflows run on the Dulwich backend
    # (dulwich==1.2.20); TO_GIT_BACKEND=cli falls back to host git as a
    # declared prerequisite. Remote worker checkouts use go-git inside
    # the dagu binary.
    ("3rdparty/caddy", "caddy"),
    ("3rdparty/dagu", "dagu"),
    ("3rdparty/allure3", "allure3"),       # npm `allure` + deps tree
    ("3rdparty/nodejs", "nodejs"),         # bundled node runtime
    # java-runtime removed: the JRE was shipped solely for Allure 2.
    # Allure 3 is Node-based; KRE carries its own runtime.
    ("3rdparty/licenses", "docs"),
]

# Hard gate: the bundle is invalid without every one of these.
REQUIRED = [
    "wsgi.py", "testorchestrator/__init__.py", "requirements.txt",
    "release/release.json", "install.sh", "scripts/install.py",
    "migrations", "deploy/Caddyfile",
    "3rdparty/caddy/caddy",
    "3rdparty/python-runtime/python/bin/python3",  # swapped for app/toctl under --frozen
    "3rdparty/dagu",
    "3rdparty/allure3",
    "3rdparty/nodejs",
]

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "instance", "dist",
                ".venv", "debs", "app-payload", "worker-payload"}


def _excluded(rel: Path) -> bool:
    parts = set(rel.parts)
    # 'dist' excludes only the repo's top-level build output — nested
    # dependency dirs like allure3/node_modules/allure/dist must survive.
    if parts & (EXCLUDE_DIRS - {"dist"}):
        return True
    if rel.parts and rel.parts[0] == "dist":
        return True
    if any(p.endswith(".pyc") or p.endswith(".egg-info")
           for p in parts):
        return True
    # No JRE for Allure any more; node_modules dev artifacts trimmed
    # by the manifest, not blanket-excluded.
    # Product-identity allowlist: the full Windows application
    # installer must never ride inside the Linux package. The
    # supplementary dagu-worker installer IS an intended download
    # asset. Nested package archives and staging dirs are excluded.
    name = rel.name
    if name.endswith(".exe") and "-setup" in name \
            and "dagu-worker" not in name:
        return True
    if name.endswith((".tar.gz", ".7z.exe")) and \
            "dagu_" not in name and "python" not in parts:
        return True
    return False


def candidate_includes(frozen: bool) -> list:
    """--frozen: the PyInstaller onedir replaces bundled CPython +
    vendored wheels, and the vendored git rootfs is dropped — frozen
    workflows are Dulwich-only (no GitPython, no bundled git, no host
    git prerequisite). The standard build keeps 3rdparty/git as the
    documented cli-backend fallback."""
    if not frozen:
        return INCLUDE
    out = [("dist/testorchestrator", "frozen-app")]
    return out + [(i, c) for i, c in INCLUDE
                  if not i.startswith("3rdparty/python")
                  and i != "3rdparty/git"]


def required_list(frozen: bool) -> list:
    reqs = [r for r in REQUIRED
            if r != "3rdparty/python-runtime/python/bin/python3"]
    return reqs + ["dist/testorchestrator/toctl"] if frozen \
        else reqs


def check_frozen_fresh() -> str | None:
    """Guard: the onedir's embedded release.json build must equal the
    repo stamp — a stale frozen dir (e.g. PyInstaller output moved or
    never refreshed) aborts the bundle instead of shipping old code."""
    emb = REPO / "dist/testorchestrator/_internal/release/release.json"
    repo = REPO / "release/release.json"
    try:
        eb = json.loads(emb.read_text()).get("build")
        rb = json.loads(repo.read_text()).get("build")
    except (OSError, json.JSONDecodeError):
        return "cannot read embedded/repo release.json for freshness check"
    if eb != rb:
        return (f"stale frozen build: onedir embeds build {eb!r} but "
                f"repo stamp is {rb!r} — rerun pyinstaller before "
                "build_bundle")
    return None


def collect(frozen: bool = False
            ) -> tuple[list[tuple[Path, str, str]], list[str]]:
    """(abs path, arc rel, category) for everything the policy includes,
    plus the list of missing REQUIRED entries."""
    files, missing = [], []
    for item, category in candidate_includes(frozen):
        src = REPO / item
        if not src.exists():
            if item in REQUIRED:
                missing.append(item)
            continue
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                if p.is_dir() and not p.is_symlink():
                    continue
                if category == "frozen-app":
                    # the onedir lands as app/ inside the package
                    rel = Path("app") / p.relative_to(src)
                    files.append((p, str(rel), category))
                    continue
                rel = p.relative_to(REPO)
                if not _excluded(rel):
                    files.append((p, str(rel), category))
        else:
            rel = src.relative_to(REPO)
            if not _excluded(rel):
                files.append((src, str(rel), category))
    for req in required_list(frozen):
        if not (REPO / req).exists():
            missing.append(req)
    return files, missing


DEPENDENCIES = {
    "kre": {
        "required_for": "test execution",
        "requirement": "operator-installed, licensed Katalon Runtime "
                       "Engine — never installed or altered by the "
                       "product",
    },
    "git": {
        "required_for": "none — workflows run on the Dulwich backend "
                        "(pure python, dulwich==1.2.20); remote worker "
                        "checkouts use go-git inside the dagu binary. "
                        "Only the opt-in TO_GIT_BACKEND=cli path needs "
                        "host git, and ssh:// portal remotes need host "
                        "ssh — declared prerequisites, not bundled",
    },
}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", action="store_true",
                    help="candidate: frozen onedir replaces "
                         "python-runtime + vendored wheels")
    args, _ = ap.parse_known_args()
    meta = json.loads((REPO / "release" / "release.json").read_text())
    plat = f"{platform.system().lower()}-{platform.machine()}"
    commit = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"], cwd=REPO,
        capture_output=True, text=True).stdout.strip() or "unknown"
    # Stamp the packaged release.json with the real build identity —
    # the checked-in file keeps `version`/`spec` only; build + source
    # revision + timestamp are generated per-build, not static.
    import datetime, tempfile
    stamped = dict(meta, build=commit, source_rev=commit,
                   built_at=datetime.datetime.now(
                       datetime.timezone.utc).isoformat())
    _stamp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(stamped, _stamp); _stamp.close()
    name = f"testorchestrator-{meta['version']}-{plat}" \
        + (f"-frozen-{commit}" if args.frozen else "")
    OUT.mkdir(exist_ok=True)
    bundle = OUT / f"{name}.tar.gz"

    if args.frozen:
        stale = check_frozen_fresh()
        if stale:
            raise SystemExit(f"bundle build FAILED — {stale}")
    # Dagu archives are fetched, not tracked: materialize the pinned
    # pair via checksum-verified download when absent.
    dagu_dir = REPO / "3rdparty" / "dagu"
    if not list(dagu_dir.glob("dagu_*_amd64.tar.gz")):
        r = subprocess.run(["bash", str(REPO / "scripts" / "fetch_dagu.sh")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("bundle build FAILED — dagu archives "
                             "missing and fetch_dagu.sh failed:\n"
                             + r.stderr + r.stdout)
    files, missing = collect(frozen=args.frozen)
    if missing:
        raise SystemExit(f"bundle build FAILED — required files missing: "
                         f"{missing}")
    t0 = time.time()
    with tarfile.open(bundle, "w:gz") as tar:
        for src, rel, _cat in files:
            if rel == "release/release.json":
                src = _stamp.name          # stamped build identity
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
    bundled = {}
    if not args.frozen:
        bundled["git"] = "vendored ubuntu-resolute-amd64 rootfs (cli-backend fallback)"
    bundled |= {
        "caddy": "pinned binary + licence + sha256",
        "dagu": "pinned 2.25.8 release tarball",
        "allure3": "pinned allure npm generator + dep tree",
        "nodejs": "pinned bundled Node.js runtime",
    }
    if args.frozen:
        bundled["frozen-app"] = ("PyInstaller onedir — toctl "
                                 "web/worker/db-upgrade/install/"
                                 "readiness/backup/recover")
    else:
        bundled["python-runtime"] = ("python-build-standalone 3.12 "
                                     "linux x86_64")
        bundled["python-wheels"] = "vendored, pip --no-index install"
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
        "includes": [i for i, _c in candidate_includes(args.frozen)],
        "bundled": bundled,
        "dependencies": DEPENDENCIES,
        "exclusions": {
            "nodejs": "bundled Node.js — no system node/npx needed; "
                            "validated by generation probe at install",
            "acquisition-debs": "3rdparty/*/debs — extracted rootfs "
                                "already packaged",
            "tests": "source-repo content, not runtime",
            "kre": "external by requirement — operator installed",
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


def verify_acquisition(manifest_path: Path,
                       stage_dir: Path) -> dict:
    """Checksum-gate acquired archives BEFORE extraction/use.
    Verifies each pinned download's checksum against the staged file —
    never silently falls back to system node/npm/allure/java.
    Returns {component: {'ok', 'path', 'sha256'|'sha512'}}; raises
    RuntimeError on missing payload or checksum mismatch."""
    import base64
    manifest = json.loads(Path(manifest_path).read_text())
    results = {}
    # nodejs archive: sha256 of the downloaded tarball
    node = manifest.get("nodejs", {})
    for plat in ("linux_x64",):
        if plat not in node:
            continue
        want = node[plat]["sha256"]
        tarball = Path(stage_dir) / "node.tar.xz"
        if not tarball.is_file():
            raise RuntimeError(
                f"missing required payload: {tarball} "
                f"(nodejs {node.get('version')})")
        got = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if got != want:
            raise RuntimeError(
                f"checksum mismatch on {tarball.name}: "
                f"expected {want[:12]}… got {got[:12]}…")
        results["nodejs"] = {"ok": True, "sha256": got}
    # allure npm tarball: sha512 from the lockfile integrity field
    allure = manifest.get("allure", {})
    if allure:
        want512 = allure["sha512"]
        tarball = Path(stage_dir) / "allure.tgz"
        if not tarball.is_file():
            raise RuntimeError(
                f"missing required payload: {tarball} "
                f"(allure {allure.get('version')})")
        got512 = base64.b64encode(
            hashlib.sha512(tarball.read_bytes()).digest()).decode()
        if got512 != want512:
            raise RuntimeError(
                f"checksum mismatch on {tarball.name}: "
                f"expected {want512[:12]}… got {got512[:12]}…")
        results["allure"] = {"ok": True, "sha512": got512}
    return results


def verify_extracted_runtime(bundle_root: Path,
                             manifest_path: Path) -> dict:
    """Completeness check on the EXTRACTED runtime in the bundle —
    distinct from the archive checksum. Fails on missing cli/binary
    so a partial stage is never silently packaged."""
    manifest = json.loads(Path(manifest_path).read_text())
    problems = []
    node_dirs = sorted(
        Path(bundle_root).glob("nodejs/node-v*/bin/node"))
    if not node_dirs and not list(
            Path(bundle_root).glob("nodejs/node*")):
        problems.append("no bundled nodejs runtime found")
    cli = manifest.get("allure", {}).get("cli",
                                         "node_modules/allure/cli.js")
    cli_path = Path(bundle_root) / "allure3" / cli
    if not cli_path.is_file():
        problems.append(f"missing allure cli: {cli_path}")
    return {"ok": not problems, "problems": problems}
