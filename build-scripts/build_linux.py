#!/usr/bin/env python3
"""One-command Linux release build.

    python3 build-scripts/build_linux.py

Prerequisites (the only ones):
  - a repository checkout of TestOrchestrator
  - python3 >= 3.12 on PATH (venv module included)
  - network access for pinned assets not already cached
  - NSIS (makensis) — auto-installed via apt-get when missing;
    sudo may prompt for a password on first run
  - TO_BUILDBOT_GPL_APPROVED=1 — required for release builds; the
    Buildbot stack (GPL) is bundled only after redistribution approval

Produces in dist/release/linux/:
  testorchestrator-<ver>-linux-x86_64.tar.gz
  testorchestrator-<ver>-linux-x86_64.run
  SHA256SUMS.txt + build-manifest.json + per-run log

The buildbot-worker NSIS installer is built when
TO_BUILDBOT_GPL_APPROVED=1 (the tarball embeds it under
packaging/windows/ for portal-side download) — never published as a
standalone release asset.

Never tags, publishes, or touches git state.
"""
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (ASSETS, BUILD_ROOT, BuildError, Log, REPO,
                     build_buildbot_worker_installer, clean_release_dir,
                     fetch_buildbot_linux_wheels,
                     finalise, git_info, release_version,
                     run, safe_extract, venv_python)


def main() -> int:
    log = Log("linux")
    try:
        log.step("TestOrchestrator Linux release build")
        gi = git_info()
        ver = release_version()
        log.write(f"version {ver} | {gi['branch']}@{gi['short']}"
                  f"{' DIRTY' if gi['dirty'] else ''}")
        if gi["commit"] == "unknown":
            raise BuildError("not a git checkout — build from a clone")
        clean_release_dir("linux")
        vpy = venv_python(BUILD_ROOT / "linux", log)

        # Buildbot worker .exe + controller wheels — GPL redistribution
        # gate: only built (and thereby bundled via packaging/windows/
        # and 3rdparty/buildbot-wheels/) when TO_BUILDBOT_GPL_APPROVED=1
        # records the approval decision.
        if os.environ.get("TO_BUILDBOT_GPL_APPROVED") == "1":
            log.step("2/5 build buildbot-worker Windows installer (NSIS) "
                     "+ controller wheels")
            bb_worker_exe = build_buildbot_worker_installer(
                BUILD_ROOT / "linux", log)
            # Controller-side stack: linux wheels install.py turns into
            # payload/buildbot-venv offline (master + local/ssh workers).
            fetch_buildbot_linux_wheels(BUILD_ROOT / "linux", log)
        else:
            bb_worker_exe = None
            log.write("  buildbot worker installer + wheels SKIPPED — "
                      "set TO_BUILDBOT_GPL_APPROVED=1 after GPL "
                      "redistribution approval")

        log.step("3/5 build release tarball + self-extracting installer")
        run([vpy, "scripts/build_bundle.py"], log)
        plat = "linux-x86_64"
        tarballs = sorted(
            (REPO / "dist").glob(f"testorchestrator-{ver}-{plat}.tar.gz"))
        if not tarballs:
            raise BuildError("build_bundle produced no tarball")
        tb = tarballs[-1]
        run_sh = tb.with_suffix("").with_suffix(".run")
        run([vpy, "scripts/build_linux_installer.py", str(tb),
             str(run_sh)], log)

        log.step("4/5 validate the assembled package")
        checks = []
        with tempfile.TemporaryDirectory() as td:
            with tarfile.open(tb, "r:gz") as tar:
                safe_extract(tar, Path(td))
            pkg = next(Path(td).glob("testorchestrator-*"))
            run([vpy, "scripts/validate_package.py", str(pkg)], log)
        checks.append({"name": "validate_package",
                       "detail": "offline deps install + vendored "
                                 "reporting-labs + worker assets "
                                 "assets checksum-verified, "
                                 "worker installer aligned)",
                       "ok": True})
        # .run round-trip: payload behind the marker must untar to the
        # same top-level dir as the tarball.
        data = run_sh.read_bytes()
        marker = b"__TO_ARCHIVE_BELOW__\n"
        idx = data.find(marker)
        if idx < 0:
            raise BuildError(".run missing __TO_ARCHIVE_BELOW__ marker")
        with tempfile.TemporaryDirectory() as td:
            with tarfile.open(fileobj=io.BytesIO(
                    data[idx + len(marker):]), mode="r:gz") as tar:
                names = {m.name.split("/")[0] for m in tar}
                safe_extract(tar, Path(td))
            if names != {tb.stem.replace(".tar", "")}:
                raise BuildError(f".run payload root mismatch: {names}")
        checks.append({"name": "run-payload-roundtrip",
                       "detail": ".run embedded tarball extracts the "
                                 "same package root", "ok": True})

        log.step("5/5 publish release outputs")
        manifest_extra = {
            "worker_assets": {},
            "buildbot_worker_assets": {
                "windows_installer_inside_package":
                    f"packaging/windows/{bb_worker_exe.name}"
                    if bb_worker_exe else None,
                "buildbot_version": ASSETS["buildbot_worker"]["version"],
                "gpl_approved": bb_worker_exe is not None,
            },
            "stale_guard": "release dir cleaned at build start; "
                           "worker-payload and older worker exes rebuilt",
        }
        # The worker exe is built (step 2/5) and ships INSIDE the
        # tarball — the portal serves it as a download. It is no
        # longer published as a standalone release asset.
        finalise(log, "linux", gi, [tb, run_sh],
                 manifest_extra, checks)
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
