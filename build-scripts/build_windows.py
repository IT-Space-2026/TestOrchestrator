#!/usr/bin/env python3
"""One-command Windows release build — FULL controller installer.

    python build-scripts\\build_windows.py

GPL redistribution gate (same as the Linux build — the payload embeds
the GPL'd Buildbot stack):
  - TO_BUILDBOT_GPL_APPROVED=1   customer-redistribution approval
                                 (recorded in the manifest)
  - TO_BUILDBOT_INTERNAL_BUILD=1 internal-only packaging test —
                                 builds everything but marks the
                                 artifact NOT approved for
                                 distribution

Prerequisites (the only ones):
  - a repository checkout of TestOrchestrator
  - Python >= 3.12 on PATH (venv module included)
  - network access for pinned assets not already cached
  - NSIS (makensis) — auto-installed via choco when present, else an
    actionable error explains the install

No git-bash, curl, unzip, or sha256sum needed — staging is pure Python.

Produces in dist/release/windows/:
  testorchestrator-<ver>-windows-amd64-setup-UNSIGNED.exe
    — the full controller: portal (Cheroot in-process frontend +
      loopback Waitress), embedded Python, vendored wheels,
      Buildbot 4.3.0 runtime (master + to-local + to-ssh-dispatcher
      workers on Task Scheduler), the bundled remote-worker installer,
      and the matching corresponding-source archive.

Never tags, publishes, or touches git state.
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (ASSETS, BUILD_ROOT, BuildError, Log, PARAMIKO_VERSION,
                     REPO, build_buildbot_worker_installer,
                     build_corresponding_sources, build_linux_worker_bundle,
                     clean_release_dir,
                     ensure_makensis, ensure_pip, fetch,
                     finalise, git_info, release_version, run,
                     unzip_safely, venv_python)

PAYLOAD = REPO / "packaging" / "windows" / "app-payload"

# Mirrors packaging/windows/app-installer.nsi payload expectations.
APP_COPY = [
    "testorchestrator", "migrations", "wsgi.py", "run_worker.py",
    "requirements.txt", "release", "deploy", "scripts",
]
# Vendored reporting-labs lands under 3rdparty/ — the same location the
# app's vendored-source bootstrap resolves in every installed layout.
VENDORED_RL = ("3rdparty/reporting_labs", "3rdparty/reporting_labs")

# Files that must NOT appear in the payload — the 2.5 controller ships
# no external proxy or legacy orchestrator component.
FORBIDDEN = ("dagu", "caddy", "Caddyfile", "provision_dagu.py")


def _stage_embedded_python(dest: Path, dl: Path, log: Log) -> None:
    unzip_safely(fetch(ASSETS["python_embed_windows"]["url"],
                       dl / "python-embed.zip",
                       ASSETS["python_embed_windows"]["sha256"], log),
                 dest)


def _wheels_to_site_packages(vpy: Path, specs: list, sp_dir: Path,
                             work: Path, log: Log) -> None:
    wheels = work / "wheels"
    run([str(vpy), "-m", "pip", "download", "--platform", "win_amd64",
         "--only-binary=:all:", "--python-version", "3.12",
         *specs, "-d", str(wheels)], log)
    sp_dir.mkdir(parents=True, exist_ok=True)
    for whl in sorted(wheels.glob("*.whl")):
        unzip_safely(whl, sp_dir)
    shutil.rmtree(wheels)


def stage_app_payload(vpy: Path, dl: Path, log: Log) -> None:
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    for d in ("python", "site-packages", "buildbot-py"):
        (PAYLOAD / d).mkdir(parents=True, exist_ok=True)

    log.write("  python embeddable (app interpreter)")
    _stage_embedded_python(PAYLOAD / "python", dl, log)

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
    for extra in ("3rdparty/licenses",
                  "3rdparty/CORRESPONDING-SOURCES.txt"):
        src, dst = REPO / extra, PAYLOAD / extra
        if not src.exists():
            raise BuildError(f"payload source missing: {extra}")
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    log.write("  worker-download assets served by the installed portal")
    # Same contract as the Linux tarball: the portal serves the bundled
    # Buildbot-worker NSIS installer AND its corresponding-source
    # archive from the installed payload — never fetched remotely.
    pkg_win = PAYLOAD / "packaging" / "windows"
    pkg_win.mkdir(parents=True, exist_ok=True)
    for pattern in ("TestOrchestrator-Buildbot-Worker-Setup-*.exe",
                    "TestOrchestrator-Buildbot-Worker-*-linux-x86_64"
                    ".tar.gz",
                    "TestOrchestrator-Buildbot-Sources-*.tar.gz"):
        matches = sorted((REPO / "packaging" / "windows").glob(pattern))
        if len(matches) != 1:
            raise BuildError(
                f"expected exactly one {pattern} under packaging/windows "
                f"— found {[m.name for m in matches]} "
                "(build it before staging)")
        shutil.copy2(matches[0], pkg_win / matches[0].name)

    log.write("  win_amd64 wheels -> site-packages (pinned requirements)")
    _wheels_to_site_packages(
        vpy, ["-r", str(REPO / "requirements.txt")],
        PAYLOAD / "site-packages", PAYLOAD, log)

    log.write("  buildbot-py: embeddable python + buildbot 4.3.0 wheels "
              "(master + workers + paramiko)")
    bbver = ASSETS["buildbot_worker"]["version"]
    _stage_embedded_python(PAYLOAD / "buildbot-py" / "python", dl, log)
    _wheels_to_site_packages(
        vpy,
        [f"buildbot[bundled]=={bbver}", f"buildbot-worker=={bbver}",
         f"buildbot-www=={bbver}", f"paramiko=={PARAMIKO_VERSION}"],
        PAYLOAD / "buildbot-py" / "site-packages",
        PAYLOAD / "buildbot-py", log)


def check_payload(log: Log) -> dict:
    """Completeness gate before makensis — the controller payload must
    contain every runtime the installer registers."""
    missing = []

    def need(rel: str) -> None:
        if not (PAYLOAD / rel).exists():
            missing.append(rel)

    for rel in ("python/python.exe",
                "testorchestrator/templates", "testorchestrator/static",
                "migrations", "wsgi.py", "run_worker.py",
                "requirements.txt",
                "release/release.json",
                "scripts/provision_buildbot.py",
                "scripts/init_env.py",
                "scripts/to_ssh_exec.py",
                "deploy/buildbot/master.cfg",
                "3rdparty/reporting_labs/__init__.py",
                "3rdparty/reporting_labs/LICENSE",
                "3rdparty/CORRESPONDING-SOURCES.txt",
                "buildbot-py/python/python.exe"):
        need(rel)
    for pattern in (
            "packaging/windows/"
            "TestOrchestrator-Buildbot-Worker-Setup-*.exe",
            "packaging/windows/"
            "TestOrchestrator-Buildbot-Worker-*-linux-x86_64.tar.gz",
            "packaging/windows/"
            "TestOrchestrator-Buildbot-Sources-*.tar.gz"):
        if not list(PAYLOAD.glob(pattern)):
            missing.append(pattern)
    # the buildbot runtime dir must carry the console-entry-point
    # packages the .cmd launchers invoke
    bb_sp = {p.name.lower()
             for p in (PAYLOAD / "buildbot-py" / "site-packages")
             .iterdir()} if (PAYLOAD / "buildbot-py" / "site-packages") \
        .exists() else set()
    for pkg in ("buildbot", "buildbot_worker", "buildbot_www",
                "paramiko"):
        if not any(d == pkg or d.startswith(pkg + "-")
                   or d.startswith(pkg + "_") for d in bb_sp):
            missing.append(f"buildbot-py/site-packages:{pkg}")

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

    # no legacy orchestrator/proxy component may ride along
    stale = [str(p.relative_to(PAYLOAD)) for p in PAYLOAD.rglob("*")
             if any(tok in p.name.lower()
                    for tok in ("dagu", "caddy"))]
    missing += stale
    if missing:
        raise BuildError("app payload incomplete:\n  "
                         + "\n  ".join(missing))
    size = sum(f.stat().st_size for f in PAYLOAD.rglob("*") if f.is_file())
    log.write(f"  payload COMPLETE: {size / 1e6:.0f} MB staged")
    return {"name": "payload-completeness", "ok": True,
            "detail": f"{len(req_names)} requirement packages + "
                      f"buildbot runtime present; {size} bytes staged"}


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

        gpl_approved = os.environ.get("TO_BUILDBOT_GPL_APPROVED") == "1"
        internal_build = (os.environ.get("TO_BUILDBOT_INTERNAL_BUILD")
                          == "1")
        if not (gpl_approved or internal_build):
            raise BuildError(
                "the Windows controller installer embeds the GPL'd "
                "Buildbot stack — set TO_BUILDBOT_GPL_APPROVED=1 after "
                "GPL redistribution approval (or "
                "TO_BUILDBOT_INTERNAL_BUILD=1 for internal packaging "
                "tests)")

        log.step("1/5 compile buildbot-worker installer (unsigned)"
                 + ("" if gpl_approved else " [INTERNAL TEST ONLY]"))
        worker_exe = build_buildbot_worker_installer(
            BUILD_ROOT / "windows", log)

        log.step("2/5 build corresponding-source archive + linux "
                 "worker bundle")
        sources_tar = build_corresponding_sources(
            BUILD_ROOT / "windows", log)
        linux_worker_tar = build_linux_worker_bundle(
            BUILD_ROOT / "windows", log)

        log.step("3/5 stage application payload")
        stage_app_payload(vpy, dl, log)
        checks = [check_payload(log)]

        log.step("4/5 compile application installer (unsigned)")
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

        log.step("5/5 verify + publish release outputs")
        bb_ver = ASSETS["buildbot_worker"]["version"]
        worker_ok = (f"Setup-{bb_ver}" in worker_exe.name)
        if not worker_ok:
            raise BuildError(f"worker installer {worker_exe.name} not "
                             f"aligned to buildbot {bb_ver}")
        checks.append({"name": "worker-version-alignment",
                       "detail": f"app + worker installers built; "
                                 f"worker embeds buildbot {bb_ver}",
                       "ok": True})
        checks.append({"name": "no-legacy-components",
                       "detail": "payload carries no dagu/caddy "
                                 "files, tasks, or config",
                       "ok": True})
        finalise(log, "windows", gi, [app_exes[0], sources_tar],
                 {"gpl_redistribution": (
                     "approved" if gpl_approved else
                     "INTERNAL TEST ONLY — not approved for "
                     "distribution"),
                  "signing": "UNSIGNED — no signing identity "
                             "configured; installers are "
                             "labelled unsigned",
                  "worker_assets": {
                      "buildbot_version": bb_ver,
                      "bundled_worker_installer": worker_exe.name,
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
