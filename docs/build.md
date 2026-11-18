# Building a release — one command

    python3 build-scripts/build_linux.py

Run from anywhere inside the checkout; paths resolve from the script's
own location, not the caller's working directory. The single pipeline
produces every release deliverable, including the Windows worker
installer — the controller itself is Linux-only.

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

No other tools are required. No git-bash, curl, unzip, sha256sum,
pip preinstall, or manual staging — everything is Python stdlib or
fetched by the script.

## What the build does

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
