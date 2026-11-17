# TestOrchestrator 2.0.3 — release notes

Status: **published** — tag `v2.0.3` → commit
`22a1400f5f17afc153a351857c3bebad47a66eac`; binaries built from that
exact clean commit. Patch release: repairs defects in the Linux
installer that prevented report generation on `install.sh` / `.run`
installations, relocates the vendored reportingLabs source to
`3rdparty/`, and ensures every installed payload carries the
worker-download assets the portal serves.

### Fixed

- **Linux installer omitted vendored reportingLabs.** Fresh `install.sh`
  (and `.run`, which executes the same installer) installations of
  2.0.1/2.0.2 could not generate reports — `report_status=failed`,
  `No module named 'reporting_labs'`. The installer now copies the
  vendored package, aborts on any missing payload source, and verifies
  required payload files after copying. Running directly from the
  extracted tarball root masked this defect.
- **Automatic upgrade repair.** Upgrading a defective 2.0.1/2.0.2
  installation installs the missing package automatically — no manual
  copy, no `PYTHONPATH`, no database migration. A superseded root-level
  `payload/reporting_labs` (including copies applied as the documented
  workaround) is removed so the package exists exactly once.
  Previously failed reports can be regenerated; configuration, users,
  execution history, evidence, and existing reports are preserved.
- **Vendored source relocated.** reportingLabs source moved from
  repository-root `reporting_labs/` to `3rdparty/reporting_labs/`
  (installed payload: `payload/3rdparty/reporting_labs/`). Import name
  `reporting_labs` unchanged; still **version 0.1.14**, MIT licence,
  bundled exactly once per runtime payload with `LICENSE` and
  `VENDORED.md` — resolved via a scoped import bootstrap, no PyPI
  download.
- **Licence page expiry label.** Stated "end date inclusive" while
  enforcement is half-open (`now < valid_until`) — now reads "runs
  must start before the expiry instant", matching the enforced
  boundary.
- **Release scope picker offered non-scopeable items.** Discovered
  test cases were listed although adding one was silently ignored —
  the picker now offers suites and collections only.
- **Installed Linux payload omitted the bundled worker installer.**
  `install.sh` now copies the `packaging/windows/` dagu-worker
  installer into the payload (superseded versions removed on
  upgrade) — previously the portal's worker-installer download
  could not be served on `install.sh`-installed systems.
- **Windows app payload omitted all worker-download assets.** The
  app installer now bundles both pinned dagu archives
  (`3rdparty/dagu/`, checksum-verified at staging) plus the NSIS
  worker installer — matching the Linux package. The build compiles
  the worker installer before payload staging so the app installer
  embeds it; `check_payload` enforces the same gate as the Linux
  `validate_package`.

### Added

- **User and administrator manuals** under
  [`Manuals-Doc/`](https://github.com/IT-Space-ltd/TestOrchestrator/tree/v2.0.3/Manuals-Doc)
  — installation, projects/bindings, workers, historical-result import,
  reports/evidence, backup/upgrade, security, troubleshooting, plus a
  validation record and 45 screenshots. Screenshots were captured on
  the **released 2.0.2 package**; the
  [validation record](https://github.com/IT-Space-ltd/TestOrchestrator/blob/v2.0.3/Manuals-Doc/Validation-Record.md)
  states which procedures ran on which version, including the
  test-licence substitution and manual reportingLabs copy used inside
  the documentation test payload (test-only — not part of the published
  installer acceptance).
- **CI `installer-validate` job** exercising the real `install.sh`
  path, closing the coverage gap that hid this defect — now also
  asserting the installed payload carries both worker-asset types.

### Changed

- **Worker installer no longer a standalone release asset.**
  `testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe` is still
  built and version-aligned with the coordinator (Dagu 2.27.0), but
  now ships only **inside** the application packages (tarball
  `packaging/windows/` + inside the Windows app installer). Download
  it from the installed portal: *System → Workers & runtimes* shows
  both worker archives and the installer with per-asset SHA-256 —
  the portal serves local files, never fetches from GitHub.

### Verified

- Clean Linux install generates and serves reports immediately
  (`reporting-labs/gen-*/index.html`), including after restart.
- `.run` self-extracts and dispatches to `install.sh`; embedded tarball
  is byte-identical to the separately published tarball.
- Upgrade from released 2.0.2 — including a seeded defective install —
  repairs report generation; previously failed reports regenerate.
- CI green on Linux (build, installer-validate, installed-validate,
  upgrade from 2.0.1 and 2.0.2) and Windows (compile, installed-
  validate, upgrade from 1.0.2 and 2.0.2); 661 tests passing.
- Installed-payload verification: all three worker downloads (both
  dagu archives + the bundled NSIS installer) served HTTP 200 with
  bytes identical to the installed payload files — no remote fetch.

### Artifacts

Built from commit `22a1400f5f17afc153a351857c3bebad47a66eac`
(dirty=false; see `build-manifest-*.json`). Dagu 2.27.0,
coordinator/worker aligned.

| Asset | Bytes | SHA-256 |
|---|---|---|
| `testorchestrator-2.0.3-linux-x86_64.tar.gz` | 278,811,947 | `2b4d97515fdc8c3e0603eaedb6f14e5fb2ce2fac9054d0157f4943e81f77b263` |
| `testorchestrator-2.0.3-linux-x86_64.run` | 278,813,951 | `7fef2b18812a17db0212f39d5f6dd7c133f9466cdbd8877ddadbb6f84bbab8de` |
| `testorchestrator-2.0.3-windows-amd64-setup-UNSIGNED.exe` | 306,850,425 | `ea7ec2fc97a4982854740001a269e7af4a5d246b3fa84412bdd417192de909a6` |

Documentation: `TestOrchestrator-Admin-Manual-2.0.3.pdf` and
`TestOrchestrator-User-Manual-2.0.3.pdf` — PDF renders of the
Manuals-Doc guides (screenshots + diagrams embedded).

Bundled (not a release asset): `testorchestrator-dagu-worker-`
`2.27.0-setup-UNSIGNED.exe`, 64,319,318 bytes, SHA-256
`f212af46ceb7c0cc121ac7fedf0d7f35f0c57c3efff49848547f2675de84e062` —
inside `packaging/windows/` of the tarball and inside the Windows
app installer; also served by the installed portal.

### Limitations

- The Windows app installer and the bundled worker installer are
  **unsigned**.
- Licensed-KRE execution remains unvalidated (no Katalon licence/API
  key available in test environments); fixture-backed runs demonstrate
  pipeline behaviour only.
- Windows interactive installation not performed; Windows validation is
  the automated CI installer suite.
- Unverified KRE threshold declarations and the Azure DevOps gate
  remain known backlog items.
