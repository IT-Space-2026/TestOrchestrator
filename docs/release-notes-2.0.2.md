# TestOrchestrator 2.0.2 — draft release notes

Status: **draft for release decision** — not published. Tag target is
the commit that introduces this file.

Focused hardening release on 2.0.1. No new feature scope: worker
execution validated end to end against the installed package,
import reliability measured and improved where measurements justified
it, evidence lifecycle closed, one-command builds preserved.

## Highlights

- **Installed-package worker E2E (Linux)**: the packaged application
  now demonstrably serves the pinned Dagu worker asset, registers a
  real `dagu worker` process, dispatches a job via the gRPC
  coordinator with exact `kre=<label>` selection, collects results,
  retains JUnit/log/screenshot evidence, and generates
  reporting-labs — validated on the generated release package, not
  the source checkout.
- **Import matching at scale**: a no-uuid import no longer rescans
  project history. Indexed `run_uuid`/`content_hash` fast paths plus
  persisted suite keys cut the worst-case preview at 800 history rows
  from ~0.35 s / ~960 queries+evidence reads to ~0.03 s / ~11
  queries+reads. Identity rules unchanged: content hash remains
  evidence, distinct uuids stay distinct executions, ambiguous inputs
  still require review.
- **Concurrent-confirm race fixed**: a project-level `ImportLock`
  plus transactional rechecks prevent two simultaneous confirmations
  of the same candidate from creating duplicate executions.
- **Retryable failures**: failed import candidates are no longer
  unretryable — staging is kept until no retryable candidates remain.
- **Evidence lifecycle closed**: execution deletion now removes
  managed local evidence (`report_dir`, `imported-evidence/<id>`)
  after the DB delete commits, with shared-evidence protection and
  traversal/symlink confinement. `toctl evidence-prune` lists orphans
  as a dry run by default; `--apply` deletes only managed,
  unreferenced directories.
- **Late-suite supplement complete**: a late-arriving collection
  suite's key now unions into the persisted suite keys so subsequent
  imports match correctly.

## Upgrade

- `db-upgrade` applies one additive migration `r8s9t0u1v2w3`
  (`executions.suite_keys_json`, `import_locks`) over retained data;
  executions, evidence, licences, schedules, credentials, and
  configuration are preserved.
- Installed upgrade verified locally: released v2.0.1
  (`52efcda5…4d488`, checksum-verified against the published release
  record) → 2.0.2 candidate on Linux — configuration, imported
  history + evidence + reporting-labs output retained, repeat-import
  dedup and new imports verified, legacy unstamped rows lazily
  fingerprinted, persistence across restarts.
- CI: `upgrade-from-v201` job added to the Linux package workflow;
  Windows build + installed-validation + upgrade jobs retained.

## Validation

- Installed-package Linux worker E2E: worker asset downloaded through
  the app route (SHA-256 verified against the pin), worker binary
  `version` = coordinator pin **2.27.0**, registered + dispatched +
  result return + evidence + reporting-labs (`2b044561`).
- Real KRE 11.7.0 dispatched through the same pipeline reached
  activation and failed with no offline licence (KRE exit 2) —
  worker log returned portal-side, classified `failed`/`incomplete`.
  **Licensed-KRE execution remains an external blocker**; this is
  recorded, not passed.
- Real-corpus import of genuine Katalon 11.7.0 results: new /
  duplicate / supplement / late-suite / failing-run paths verified;
  reporting-labs `ready` with real HAR/log evidence attached.
- Full test suite: **631 passed, 1 skipped** (the skip is the
  unchanged pre-existing conditional skip; 13 new hardening tests).
- Benchmark: `python3 scripts/benchmark_import.py` — before/after
  numbers in `docs/next-2.0.2.md`.

## Artifacts

Built from the tag commit by `build-scripts/`. Names, byte sizes and
SHA-256 digests are recorded in `SHA256SUMS.txt` and
`build-manifest.json` alongside the artifacts in `dist/release/`.

- `testorchestrator-2.0.2-linux-x86_64.tar.gz`
- `testorchestrator-2.0.2-linux-x86_64.run` (self-extracting)
- `testorchestrator-2.0.2-windows-amd64-setup-UNSIGNED.exe`
  (**unsigned** — Windows builds are not code-signed)
- `testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe`
  (**unsigned**)

## Deferred checks and limitations (explicit, not passed)

- **Licensed-KRE execution** — no Katalon licence/apiKey available on
  the validation host; real dispatch verified through activation
  failure only. The unblocked pipeline (worker download → register →
  dispatch → result/evidence/rlabs) is demonstrated with a fixture
  KRE emitting genuine Katalon-format output; this is not claimed as
  licensed end-to-end execution.
- **Windows interactive installation, GUI, and real-corpus
  execution** — deferred; never attempted for 2.0.2. Windows CI
  (compile + installed validation + upgrade jobs) is retained and
  reported; the Windows installers remain unsigned.
- Non-KRE JUnit imports land as `framework='unknown'` (unchanged).
- KRE options gating thresholds are declared, not verified.
- Azure DevOps repo interoperability remains gated on an authorized
  repository.
