#!/usr/bin/env python3
"""One-command Windows release build.

    python build-scripts\\build_windows.py

Prerequisites (the only ones):
  - a repository checkout of TestOrchestrator
  - Python >= 3.12 on PATH (venv module included)
  - network access for pinned assets not already cached
  - NSIS (makensis) — auto-installed via choco when present, else an
    actionable error explains the install

No git-bash, curl, unzip, or sha256sum needed — staging is pure Python.

Produces in dist/release/windows/:
  testorchestrator-<ver>-windows-amd64-setup-UNSIGNED.exe
  TestOrchestrator-Buildbot-Worker-Setup-<bb>-UNSIGNED.exe (via linux build)
  SHA256SUMS.txt + build-manifest.json + per-run log

Never tags, publishes, or touches git state.
"""
import shutil
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (ASSETS, BUILD_ROOT, BuildError, Log, REPO,
                     build_buildbot_worker_installer, clean_release_dir,
                     ensure_makensis, ensure_pip, fetch,
                     finalise, git_info, release_version, run,
                     sha256_file, unzip_safely, venv_python,
                     read_pin_file)

PAYLOAD = REPO / "packaging" / "windows" / "app-payload"

# Mirrors packaging/windows/app-installer.nsi payload expectations and
# build_app_installer.sh's staging set.
APP_COPY = [
    "testorchestrator", "migrations", "wsgi.py", "run_worker.py",
    "requirements.txt", "release", "deploy",
]
# Vendored reporting-labs lands under 3rdparty/ — the same location the
# app's vendored-source bootstrap resolves in every installed layout.
VENDORED_RL = ("3rdparty/reporting_labs", "3rdparty/reporting_labs")


def stage_app_payload(vpy: Path, dl: Path, log: Log) -> None:
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    for d in ("python", "site-packages", "wheels"):
        (PAYLOAD / d).mkdir(parents=True, exist_ok=True)

    log.write("  python embeddable")
    unzip_safely(fetch(ASSETS["python_embed_windows"]["url"],
                       dl / "python-embed.zip",
                       ASSETS["python_embed_windows"]["sha256"], log),
                 PAYLOAD / "python")

    log.write("  application source")
    for item in APP_COPY:
        src = REPO / item
        dst = PAYLOAD / item
        if not src.exists():
            raise BuildError(f"payload source missing: {item}")
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".pytest_cache", "instance"))
        else:
            shutil.copy2(src, dst)
    src, dst = REPO / VENDORED_RL[0], PAYLOAD / VENDORED_RL[1]
    if not src.is_dir():
        raise BuildError(f"payload source missing: {VENDORED_RL[0]}")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc"))
    log.write("  worker-download assets served by the installed portal")
    # Same contract as the Linux tarball: the portal serves the
    # bundled Buildbot-worker NSIS installer from the installed
    # payload — never fetched remotely at runtime.
    worker_exes = sorted((REPO / "packaging" / "windows").glob(
        "TestOrchestrator-Buildbot-Worker-Setup-*.exe"))
    if len(worker_exes) != 1:
        raise BuildError(
            "expected exactly one built buildbot-worker installer "
            f"under packaging/windows — found "
            f"{[e.name for e in worker_exes]} (build it before staging)")
    pkg_win = PAYLOAD / "packaging" / "windows"
    pkg_win.mkdir(parents=True, exist_ok=True)
    shutil.copy2(worker_exes[0], pkg_win / worker_exes[0].name)

    log.write("  win_amd64 wheels -> site-packages (pinned requirements)")
    ensure_pip(vpy, log)
    run([vpy, "-m", "pip", "download", "--platform", "win_amd64",
         "--only-binary=:all:", "--python-version", "3.12",
         "-r", str(REPO / "requirements.txt"),
         "-d", str(PAYLOAD / "wheels")], log)
    for whl in sorted((PAYLOAD / "wheels").glob("*.whl")):
        unzip_safely(whl, PAYLOAD / "site-packages")
    shutil.rmtree(PAYLOAD / "wheels")


def check_payload(log: Log) -> dict:
    """Port of check_payload.sh — completeness gate before makensis."""
    missing = []

    def need(rel: str) -> None:
        if not (PAYLOAD / rel).exists():
            missing.append(rel)

    for rel in ("python/python.exe",
                "testorchestrator/templates", "testorchestrator/static",
                "migrations", "wsgi.py", "requirements.txt",
                "release/release.json",
                "3rdparty/reporting_labs/__init__.py",
                "3rdparty/reporting_labs/LICENSE"):
        need(rel)
    # Worker-download assets the installed portal must serve — same
    # completeness contract as validate_package.py on the Linux side.
    worker_exes = list((PAYLOAD / "packaging" / "windows").glob(
        "TestOrchestrator-Buildbot-Worker-Setup-*.exe"))
    if not worker_exes:
        missing.append("packaging/windows/"
                       "TestOrchestrator-Buildbot-Worker-Setup-*.exe")
    elif len(worker_exes) > 1:
        missing.append("packaging/windows/ multiple worker installers: "
                       f"{[e.name for e in worker_exes]}")
    req_names = [l.split("==")[0].split(">=")[0].strip()
                 for l in (PAYLOAD / "requirements.txt").read_text()
                 .splitlines()
                 if l.strip() and not l.startswith(("#", "-"))]
    # match package dirs, dist-info dirs, and single-module .py files —
    # dist names normalise to underscores but installed names vary
    # (argon2-cffi -> argon2/, typing_extensions -> typing_extensions.py).
    sp = {p.name.lower() for p in (PAYLOAD / "site-packages").iterdir()}
    for name in req_names:
        norm = name.lower().replace("-", "_")
        if not any(d == norm or d.startswith(
                (norm + "-", norm + "_", norm + ".")) for d in sp):
            missing.append(f"site-packages:{name}")
    if missing:
        raise BuildError("app payload incomplete:\n  "
                         + "\n  ".join(missing))
    size = sum(f.stat().st_size for f in PAYLOAD.rglob("*") if f.is_file())
    log.write(f"  payload COMPLETE: {size / 1e6:.0f} MB staged")
    return {"name": "payload-completeness", "ok": True,
            "detail": f"{len(req_names)} requirement packages present; "
                      f"{size} bytes staged"}


def main() -> int:
    # Windows CONTROLLER packaging is pending the Buildbot port: the
    # payload needs a Windows master/local-worker provisioning story
    # (provision_buildbot + win wheels + schtasks units) replacing the
    # removed dagu.exe/caddy.exe staging below. The Windows WORKER
    # installer — the artefact the release actually requires — is
    # produced by build_linux.py (build_buildbot_worker_installer).
    import sys as _s
    _s.stderr.write(
        "windows controller build: pending Buildbot port — dagu/caddy "
        "payload staging removed; worker .exe comes from "
        "build_linux.py\n")
    return 1
    # unreachable — kept for the pending rework
    log = Log("windows")
    try:
        log.step("TestOrchestrator Windows release build")
        gi = git_info()
        ver = release_version()
        log.write(f"version {ver} | {gi['branch']}@{gi['short']}"
                  f"{' DIRTY' if gi['dirty'] else ''}")
        if gi["commit"] == "unknown":
            raise BuildError("not a git checkout — build from a clone")
        clean_release_dir("windows")
        vpy = venv_python(BUILD_ROOT / "windows", log)
        dl = BUILD_ROOT / "downloads"
        dl.mkdir(parents=True, exist_ok=True)
        makensis = ensure_makensis(log)

        log.step("1/4 compile buildbot-worker installer (unsigned)")
        worker_exe = build_buildbot_worker_installer(
            BUILD_ROOT / "windows", log)

        log.step("2/4 stage application payload")
        stage_app_payload(vpy, dl, log)
        checks = [check_payload(log)]

        log.step("3/4 compile application installer (unsigned)")
        win = REPO / "packaging" / "windows"
        # only same-version outputs are refreshed — older local builds
        # are left alone (the bundle's own exclusion policy decides
        # what ships, not filename cleanup).
        for stale in win.glob(
                f"testorchestrator-{ver}-windows-amd64-*.exe"):
            stale.unlink()
        run([makensis, "-V2", f"-DVERSION={ver}",
             "app-installer.nsi"], log, cwd=win)
        app_exes = sorted(win.glob(
            f"testorchestrator-{ver}-windows-amd64-setup-UNSIGNED.exe"))
        if not app_exes:
            raise BuildError("makensis produced no app installer")
        log.write(f"  built {app_exes[0].name} "
                  f"({app_exes[0].stat().st_size:,} B, UNSIGNED)")

        log.step("4/4 verify + publish release outputs")
        bb_ver = ASSETS["buildbot_worker"]["version"]
        worker_ok = (f"Setup-{bb_ver}" in worker_exe.name)
        if not worker_ok:
            raise BuildError(f"worker installer {worker_exe.name} not "
                             f"aligned to buildbot {bb_ver}")
        checks.append({"name": "worker-version-alignment",
                       "detail": f"app + worker installers built; "
                                 f"worker embeds buildbot {bb_ver}",
                       "ok": True})
        finalise(log, "windows", gi, [app_exes[0]],
                       {"signing": "UNSIGNED — no signing identity "
                                   "configured; installers are "
                                   "labelled unsigned",
                        "worker_assets": {
                            "buildbot_version": bb_ver,
                            "bundled_worker_installer":
                                f"packaging/windows/{worker_exe.name}",
                            "note": "worker installer is built and "
                                    "bundled INSIDE the app installer "
                                    "for portal-side download — not "
                                    "published as a standalone "
                                    "release asset"}},
                       checks)
        log.close()
        return 0
    except BuildError as e:
        log.write(f"\nBUILD FAILED\n{e}")
        log.close()
        return 1
    except KeyboardInterrupt:
        log.write("\ninterrupted")
        log.close()
        return 130


if __name__ == "__main__":
    sys.exit(main())
