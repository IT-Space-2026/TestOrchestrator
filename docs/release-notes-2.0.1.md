# TestOrchestrator 2.0.1 — draft release notes

Status: **draft for release decision** — not published. Tag target is
the commit that introduces this file.

## Highlights

- **Repository-result import**: authenticated, project-scoped
  preview → confirm workflow imports existing Katalon KRE results
  (and generic JUnit XML) from bound repositories into execution
  history — no test execution, no background imports.
- **Duplicate prevention**: identity is the producer `execution.uuid`
  plus a canonical content fingerprint used as *evidence*. Collection
  runs sharing one uuid merge into a single execution; distinct
  reruns — including reruns with byte-identical outcomes — stay
  separate; conflicting results under one uuid require review;
  late-arriving suites supplement safely; imports never cross
  project boundaries.
- **History integration**: imported runs appear in the same
  history/detail views with an `imported` origin badge, provenance
  (repo, commit, source path, parser, import timestamp), warnings,
  and new `origin`/`framework` filters. Cancel/Retry are not offered
  for imported runs. reportingLabs generates from imported runs.
- **One-command builds**: `python3 build-scripts/build_linux.py` and
  `python build-scripts\build_windows.py` produce the complete
  platform release — pinned+verified downloads, worker installers,
  packages, checksums, manifests, logs — with no manual steps.
- **Framework-independent foundations**: `result_format` (junit) is
  separated from `framework` (katalon | unknown) so future Cypress/
  Robot/Playwright parsers land on the same model. Non-KRE JUnit
  imports as `framework='unknown'`.

## Upgrade

- `db-upgrade` applies additive migration `q7r8s9t0u1v2` over
  retained data; executions, evidence, licence, schedules, and
  credentials are preserved.
- Installed-upgrade verified: released v2.0.0 → this build on Linux
  (local, real install) and on both platforms in CI
  (`upgrade-from-v200` jobs), plus the pre-existing v1.0.2 upgrade
  check.

## Artifacts

Built from the tag commit by `build-scripts/`. Names, byte sizes and
SHA-256 digests are recorded in `SHA256SUMS.txt` and
`build-manifest.json` alongside the artifacts in `dist/release/`.

- `testorchestrator-2.0.1-linux-x86_64.tar.gz`
- `testorchestrator-2.0.1-linux-x86_64.run` (self-extracting)
- `testorchestrator-2.0.1-windows-amd64-setup-UNSIGNED.exe`
  — unsigned; signing requires credentials not in the repo
- `testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe` (Windows
  worker installer)

All packages bundle Dagu 2.27.0 coordinator+worker assets (both
pinned archives, checksum-verified) and vendored reporting-labs
0.1.14.

## Known limitations

- Windows interactive GUI and real-corpus end-to-end checks remain
  deferred — not passed.
- Import matching scans project history at classify time.
- Generic JUnit imports land as `framework='unknown'` until
  framework-specific parsers exist.
- Import is manual (no scheduled/automatic imports) and never
  executes tests.
