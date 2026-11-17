# TestOrchestrator 2.0.3 — release notes

Status: **published** — tag `v2.0.3` → commit
`60273c78dffa35ecf2988b28caf6ef2c0a10c0d1`; binaries built from that
exact clean commit. Patch release: repairs a defect in the Linux
installer that prevented report generation on `install.sh` / `.run`
installations, and relocates the vendored reportingLabs source to
`3rdparty/`.

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

### Added

- **User and administrator manuals** under
  [`Manuals-Doc/`](https://github.com/IT-Space-2026/TestOrchestrator/tree/v2.0.3/Manuals-Doc)
  — installation, projects/bindings, workers, historical-result import,
  reports/evidence, backup/upgrade, security, troubleshooting, plus a
  validation record and 25 screenshots. Screenshots were captured on
  the **released 2.0.2 package**; the
  [validation record](https://github.com/IT-Space-2026/TestOrchestrator/blob/v2.0.3/Manuals-Doc/Validation-Record.md)
  states which procedures ran on which version, including the
  test-licence substitution and manual reportingLabs copy used inside
  the documentation test payload (test-only — not part of the published
  installer acceptance).
- **CI `installer-validate` job** exercising the real `install.sh`
  path, closing the coverage gap that hid this defect.

### Verified

- Clean Linux install generates and serves reports immediately
  (`reporting-labs/gen-*/index.html`), including after restart.
- `.run` self-extracts and dispatches to `install.sh`; embedded tarball
  is byte-identical to the separately published tarball.
- Upgrade from released 2.0.2 — including a seeded defective install —
  repairs report generation; previously failed reports regenerate.
- CI green on Linux (build, installer-validate, installed-validate,
  upgrade from 2.0.1 and 2.0.2) and Windows (compile, installed-
  validate, upgrade from 1.0.2 and 2.0.2); 637 tests passing.

### Artifacts

Built from commit `60273c78dffa35ecf2988b28caf6ef2c0a10c0d1`
(dirty=false; see `build-manifest-*.json`). Dagu 2.27.0,
coordinator/worker aligned.

| Asset | Bytes | SHA-256 |
|---|---|---|
| `testorchestrator-2.0.3-linux-x86_64.tar.gz` | 278,810,852 | `1dadeabe2c2728d5dcc1ed6a6bfd5a403a68ef4ccd7f884ff82e2a0f25161405` |
| `testorchestrator-2.0.3-linux-x86_64.run` | 278,812,856 | `44936efe833847b0c0d58c1b0cdfc5149510999e9e8dcadd51d0d2432ded4d64` |
| `testorchestrator-2.0.3-windows-amd64-setup-UNSIGNED.exe` | 113,510,358 | `719553d08efb43063b49f00888cf565b32562300fd7d7fbc7c7fd6224a605670` |
| `testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe` | 64,319,318 | `f212af46ceb7c0cc121ac7fedf0d7f35f0c57c3efff49848547f2675de84e062` |

### Limitations

- Both Windows executables are **unsigned**.
- Licensed-KRE execution remains unvalidated (no Katalon licence/API
  key available in test environments); fixture-backed runs demonstrate
  pipeline behaviour only.
- Windows interactive installation not performed; Windows validation is
  the automated CI installer suite.
- Unverified KRE threshold declarations and the Azure DevOps gate
  remain known backlog items.
