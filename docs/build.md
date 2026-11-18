# Building a release — one command per platform

    python3 build-scripts/build_linux.py      # Linux controller package
    python3 build-scripts/build_windows.py    # Windows controller installer

Run from anywhere inside the checkout; paths resolve from the script's
own location, not the caller's working directory. The Linux pipeline
produces the Linux deliverables plus the bundled Windows worker
installer; the Windows pipeline produces the full Windows controller
installer (`testorchestrator-<v>-windows-amd64-setup-UNSIGNED.exe`),
which embeds the same worker installer and corresponding-source
archive. Both are unsigned (`-UNSIGNED` in the name).

`TO_BUILDBOT_GPL_APPROVED` gates the bundling of the GPL components
(Buildbot packages + Paramiko) into customer artifacts. Unset, the
build produces an internal-test package whose manifest marks the GPL
payload unapproved; set `1` only after the redistribution checks in
`docs/redistribution-review.md` are satisfied.

## Prerequisites — the complete list

| Requirement | Notes |
|---|---|
| repository checkout | `git clone`; the build stamps the exact commit and reports a dirty tree — it never switches branches or fetches another revision |
| Python ≥ 3.12 on PATH | stdlib only; `venv` module must exist (Debian/Ubuntu: `python3` is enough — a missing `ensurepip`/`python3-venv` is handled automatically) |
| network access | pinned assets are downloaded on first build and cached in `build/downloads/` |
| NSIS (`makensis`) | **optional to preinstall** — the script installs it: `apt-get install nsis` via sudo on Debian/Ubuntu, or a no-root user-space extraction of the `nsis`+`nsis-common` debs into `build/tools/` |

Both pipelines run on Linux — makensis + the pip `--platform
win_amd64` wheel staging are cross-platform, so the Windows
installer builds from the same checkout without a Windows host.

No other tools are required. No git-bash, curl, unzip, sha256sum,
pip preinstall, or manual staging — everything is Python stdlib or
fetched by the script.

## What the Linux build does

`build_linux.py`:
1. creates an isolated build venv under `build/linux/venv` (pip
   bootstrapped inside when ensurepip is absent)
2. fetches the pinned Buildbot/Paramiko sdists, the Windows
   Python-embeddable zip, and the Windows worker wheel set — all
   sha256-verified (`build-scripts/assets.json`)
3. builds `TestOrchestrator-Buildbot-Sources-<v>.tar.gz` — the
   corresponding-source archive (exact upstream sdists + packaging,
   build, and install scripts + notices)
4. stages `packaging/windows/bb-worker-payload/` and compiles the
   Windows **Buildbot worker installer** via NSIS — it ships inside
   the Linux package and is served by the installed portal
5. `scripts/build_bundle.py` → `dist/testorchestrator-<v>-linux-x86_64.tar.gz`
6. `scripts/build_linux_installer.py` → self-extracting `.run`
7. validates: `scripts/validate_package.py` on the extracted tarball
   (offline dep install, vendored reporting-labs, worker installer +
   corresponding-source archive present, controller/worker Buildbot
   version alignment) + `.run` payload round-trip
8. publishes deliverables + `SHA256SUMS.txt` + `build-manifest.json`

## Output

`dist/release/linux/` — final deliverables only:

    testorchestrator-<v>-linux-x86_64.tar.gz
    testorchestrator-<v>-linux-x86_64.run
    TestOrchestrator-Buildbot-Sources-<v>.tar.gz
    SHA256SUMS.txt                     (incl. the bundled worker .exe)
    build-manifest.json

`dist/release/windows/`:

    testorchestrator-<v>-windows-amd64-setup-UNSIGNED.exe
    TestOrchestrator-Buildbot-Sources-<v>.tar.gz
    SHA256SUMS.txt
    build-manifest.json

The Windows installer payload (staged under
`packaging/windows/app-payload/`) carries: embedded Python +
`site-packages` (portal + Cheroot/Waitress), a second embedded
`buildbot-py` runtime (buildbot master/worker + paramiko wheels),
the application source, `scripts/` (provision + SSH dispatcher +
`init_env.py`), `deploy/buildbot/master.cfg`, the bundled worker
installer, and the corresponding-source archive.
`packaging/windows/app-installer.nsi` wires `%ProgramData%` state,
five Scheduled Tasks (web + dispatch worker + buildbot master +
local + SSH dispatcher), per-install CA trust, and retention-safe
uninstall — no external web/proxy component.

`dist/release/logs/` holds per-run build logs. `build/` holds
transient staging (venvs, downloads cache, extracted NSIS). Both are
gitignored; re-running needs no cleanup.

`build-manifest.json` records: release version, source commit +
dirty flag + branch, tool versions, every artifact's size + sha256,
the GPL-approval flag state, and the validation checks that ran.

## Version + pin sources

| Pin | Authoritative source |
|---|---|
| release version | `release/release.json` → `version` |
| Buildbot/Paramiko sdists, python-embed zip, get-pip, Windows wheels | `build-scripts/assets.json` |
| python deps | `requirements.txt` (exact `==` pins) |

The `.nsi` scripts accept `-DVERSION=`; their inline defaults apply
only to manual makensis runs.

## Worker inventory reconciliation

`scripts/validate_package.py` gates the package: exactly one
`TestOrchestrator-Buildbot-Worker-Setup-<v>-UNSIGNED.exe` **and** one
matching `TestOrchestrator-Buildbot-Sources-<v>.tar.gz` must exist
under `packaging/windows/`, and the worker payload must pin the same
Buildbot version as the controller. A missing/skewed asset fails the
build — nothing is silently omitted.

## Failure behaviour

- non-zero exit on any failed step; the last ~15 lines of the failing
  command are shown, the full log is under `dist/release/logs/`
- corrupt/stale downloads are refetched; a checksum mismatch on a
  fresh download aborts the build
- the scripts never tag, publish, push, modify git state, or delete
  user data — only `build/` and `dist/` content they own

## Known gaps

- The Windows worker `.exe` is compiled on Linux and not executed
  there — installed-payload checks run in CI and Windows acceptance
  runs post-release on a real Windows host.
