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
  testorchestrator-dagu-worker-<dagu>-setup-UNSIGNED.exe
  SHA256SUMS.txt + build-manifest.json + per-run log

Never tags, publishes, or touches git state.
"""
import shutil
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (ASSETS, BUILD_ROOT, BuildError, Log, REPO,
                     build_worker_installer, clean_release_dir,
                     ensure_makensis, ensure_pip, fetch, fetch_dagu,
                     finalise, git_info, release_version, run,
                     sha256_file, unzip_safely, venv_python)

PAYLOAD = REPO / "packaging" / "windows" / "app-payload"

# Mirrors packaging/windows/app-installer.nsi payload expectations and
# build_app_installer.sh's staging set.
APP_COPY = [
    "testorchestrator", "migrations", "wsgi.py", "run_worker.py",
    "requirements.txt", "release", "deploy", "reporting_labs",
]


def stage_app_payload(vpy: Path, dl: Path, log: Log) -> None:
    win = REPO / "packaging" / "windows"
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    for d in ("python", "dagu", "caddy", "site-packages", "wheels"):
        (PAYLOAD / d).mkdir(parents=True, exist_ok=True)

    log.write("  python embeddable + caddy windows")
    unzip_safely(fetch(ASSETS["python_embed_windows"]["url"],
                       dl / "python-embed.zip",
                       ASSETS["python_embed_windows"]["sha256"], log),
                 PAYLOAD / "python")
    unzip_safely(fetch(ASSETS["caddy_windows"]["url"],
                       dl / "caddy-windows.zip",
                       ASSETS["caddy_windows"]["sha256"], log),
                 PAYLOAD / "caddy")

    log.write("  dagu.exe from pinned windows archive")
    archive = fetch_dagu("windows_amd64", log)
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            if Path(m.name).name in {"dagu.exe", "LICENSE"} and m.isfile():
                m.name = Path(m.name).name
                tar.extract(m, PAYLOAD / "dagu", filter="data")
    if not (PAYLOAD / "dagu" / "dagu.exe").is_file():
        raise BuildError("dagu.exe not found in pinned windows archive")

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
    prov = REPO / "scripts" / "provision_dagu.py"
    shutil.copy2(prov, PAYLOAD / "provision_dagu.py")

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

    for rel in ("python/python.exe", "dagu/dagu.exe", "caddy/caddy.exe",
                "testorchestrator/templates", "testorchestrator/static",
                "migrations", "wsgi.py", "requirements.txt",
                "release/release.json", "dagu/LICENSE",
                "reporting_labs", "provision_dagu.py"):
        need(rel)
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

        log.step("1/4 stage application payload")
        stage_app_payload(vpy, dl, log)
        checks = [check_payload(log)]

        log.step("2/4 compile application installer (unsigned)")
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

        log.step("3/4 compile dagu-worker installer (unsigned)")
        worker_exe = build_worker_installer(log, ASSETS["dagu"]["version"])

        log.step("4/4 verify + publish release outputs")
        dagu_ver = ASSETS["dagu"]["version"]
        worker_ok = (f"-{dagu_ver}-" in worker_exe.name)
        if not worker_ok:
            raise BuildError(f"worker installer {worker_exe.name} not "
                             f"aligned to dagu {dagu_ver}")
        checks.append({"name": "worker-version-alignment",
                       "detail": f"app + worker installers built; "
                                 f"worker embeds dagu {dagu_ver}",
                       "ok": True})
        out = finalise(log, "windows", gi, [app_exes[0], worker_exe],
                       {"signing": "UNSIGNED — no signing identity "
                                   "configured; installers are "
                                   "labelled unsigned",
                        "worker_assets": {
                            "dagu_version": dagu_ver,
                            "note": "worker installer is a standalone "
                                    "release asset; the Linux package "
                                    "embeds it for portal-side download"}},
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
