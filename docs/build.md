# Building a release — one command per platform

Supported entry points (added in 2.0.1):

    Windows:  python build-scripts\build_windows.py
    Linux:    python3 build-scripts/build_linux.py

Run from anywhere inside the checkout; paths resolve from the script's
own location, not the caller's working directory.

## Prerequisites — the complete list

| Requirement | Notes |
|---|---|
| repository checkout | `git clone`; the build stamps the exact commit and reports a dirty tree — it never switches branches or fetches another revision |
| Python ≥ 3.12 on PATH | stdlib only; `venv` module must exist (Debian/Ubuntu: `python3` is enough — a missing `ensurepip`/`python3-venv` is handled automatically) |
| network access | pinned assets are downloaded on first build and cached in `build/downloads/` |
| NSIS (`makensis`) | **optional to preinstall** — the script installs it: `apt-get install nsis` via sudo on Debian/Ubuntu, `choco install nsis` on Windows, or a no-root user-space extraction of the `nsis`+`nsis-common` debs into `build/tools/` (Linux) |

No other tools are required. No git-bash, curl, unzip, sha256sum,
pip preinstall, or manual staging — everything is Python stdlib or
fetched by the script.

## What each build does

`build_linux.py`:
1. creates an isolated build venv under `build/linux/venv` (pip
   bootstrapped inside when ensurepip is absent)
2. fetches the pinned dagu archives into `3rdparty/dagu/`
   (sha256-verified against `3rdparty/dagu/dagu.sha256`)
3. stages `worker-payload/` and compiles the Windows **dagu-worker
   installer** via NSIS — it ships inside the Linux package
4. `scripts/build_bundle.py` → `dist/testorchestrator-<v>-linux-x86_64.tar.gz`
5. `scripts/build_linux_installer.py` → self-extracting `.run`
6. validates: `scripts/validate_package.py` on the extracted tarball
   (offline dep install, vendored reporting-labs, worker assets,
   coordinator/worker version alignment) + `.run` payload round-trip
7. publishes deliverables + `SHA256SUMS.txt` + `build-manifest.json`

`build_windows.py`:
1. isolated build venv under `build/windows/venv` (same bootstrap)
2. fetches pinned Python-embeddable zip, Caddy-windows zip, and the
   dagu Windows archive — all sha256-verified (`assets.json` +
   `dagu.sha256`)
3. stages `packaging/windows/app-payload/` entirely in Python:
   runtimes, `dagu.exe`+`LICENSE`, app source, vendored
   `reporting_labs/`, and every `requirements.txt` wheel resolved for
   `win_amd64`/py3.12 into `site-packages/`
4. payload completeness gate (runtimes, app dirs, every requirement
   present — package dirs, dist-info, and module files)
5. `makensis -DVERSION=<release.json version>` →
   `testorchestrator-<v>-windows-amd64-setup-UNSIGNED.exe`
6. dagu-worker installer via the same shared staging
7. alignment check + deliverables + checksums + manifest

## Output

`dist/release/<platform>/` — final deliverables only:

    dist/release/linux/    testorchestrator-<v>-linux-x86_64.tar.gz
                           testorchestrator-<v>-linux-x86_64.run
                           testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe
    dist/release/windows/  testorchestrator-<v>-windows-amd64-setup-UNSIGNED.exe
                           testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe
    both:                  SHA256SUMS.txt, build-manifest.json
    dist/release/logs/     per-run build logs

`build/` holds transient staging (venvs, downloads cache, extracted
NSIS). Both are gitignored; re-running needs no cleanup — previous
deliverables for that platform are replaced, caches and other
platforms' outputs are kept.

`build-manifest.json` records: release version, source commit +
dirty flag + branch, tool versions (python, makensis, dagu, caddy,
python-embed), every artifact's size + sha256, and the validation
checks that ran.

## Version + pin sources

| Pin | Authoritative source |
|---|---|
| release version | `release/release.json` → `version` |
| dagu archives | `3rdparty/dagu/dagu.sha256` digests; URL/version in `build-scripts/assets.json` |
| caddy (linux) | `3rdparty/caddy/caddy.sha256` |
| caddy (windows), python-embed zip, get-pip | `build-scripts/assets.json` |
| python deps | `requirements.txt` (exact `==` pins) |

The `.nsi` scripts accept `-DVERSION=`; their inline defaults apply
only to manual makensis runs.

## Worker inventory reconciliation

The Linux package's worker assets are gated by
`scripts/validate_package.py`: both pinned dagu archives must exist,
checksum-verify, and share one version; exactly one
`testorchestrator-dagu-worker-<same version>-setup-UNSIGNED.exe` must
be present under `packaging/windows/`. A missing/skewed worker asset
fails the build — nothing is silently omitted. The Windows release
produces the same worker installer as a standalone artifact alongside
the app installer.

## Failure behaviour

- non-zero exit on any failed step; the last ~15 lines of the failing
  command are shown, the full log is under `dist/release/logs/`
- corrupt/stale downloads are refetched; a checksum mismatch on a
  fresh download aborts the build
- the scripts never tag, publish, push, modify git state, or delete
  user data — only `build/` and `dist/` content they own

## Known gaps

- The Windows artifacts produced by `build_windows.py` are compiled
  but not executed on this host — installation/startup smoke runs in
  `windows-installer.yml` CI (installed-validate + upgrade-from-v102
  jobs). Size differences vs CI-built installers reflect the NSIS
  toolchain version, not payload differences (payload is gate-checked).
- The legacy `packaging/windows/build_*.sh` scripts still work for
  manual debugging but are superseded — pins may drift from
  `assets.json`.
