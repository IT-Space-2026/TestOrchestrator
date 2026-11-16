# 2.0.1 — deferred scope, brought forward post-2.0.0 release

Created at v2.0.0 publication. Two features deferred out of 2.0.0;
implementation on branch `feature/2.0.1` (from tag `v2.0.0`/`cdc4a91`).

## Implementation plan (updated during build)

### A. Repository-result import

Real KRE output inspected (live remote-run evidence):
`Reports/<YYYYMMDD_HHMMSS>/<Suite>/<ts>/` containing
`JUnit_Report.xml` (structured results + properties: suite id,
timestamp, host, katalonVersion, projectName), `execution0.log`,
`console0.log`, `execution.properties` (run metadata),
`execution.uuid` (KRE-side run identifier — primary dedup key),
`testCaseBinding`, `<ts>.html`, `<ts>.csv`, `tsc_id.txt`, `requests/`.

- **Supported inputs**: KRE report directories (the layout above) and
  bare JUnit XML matching the Katalon schema. HTML/CSV are evidence
  artifacts only — never parsed for results (no structured data).
- **Grouping**: one `Reports/<ts>/<Suite>/<ts>/` tree = one execution
  per suite; a multi-suite collection run is one execution with N
  suite results sharing the outer timestamp dir.
- **Identifiers/dedup**: `execution.uuid` when present; else
  content-hash key over the suite's JUnit XML + properties
  (project, suite id, start timestamp, per-case identity) — never
  filename/commit/test-count alone. Cross-checked against live
  executions via their stored remote-run report hash/uuid.
- **Schema**: `executions.origin` (live|imported), `imported_at`,
  `imported_by`, `source_repo`, `source_commit`, `source_path`,
  `content_hash`, `run_uuid`; results unchanged; `import_warnings`/
  provenance table for parser version + per-input status.
  Alembic migration, additive only.
- **rlabs rendering**: outcomes, durations, suite/case names,
  failure text from JUnit `<system-out>`; artifacts (logs, html,
  screenshots under `requests/`) as evidence links. Missing pieces
  (env, runner, snapshot) shown as unavailable — report generation
  explains gaps rather than inventing them.

### B. One-command builds (`build-scripts/`)

Current flow consolidated (not reinvented):

| Step | Was | Now |
|---|---|---|
| asset fetch | `scripts/fetch_dagu.sh`, inline curls in `.sh` | Python `urllib` + sha256 verify vs `assets.json` + existing `*.sha256` files |
| worker installer (Linux side) | `build_worker_installer.sh` | shared `_common.stage_worker_payload` + `makensis -DVERSION` |
| tarball | `build_bundle.py` | invoked by `build_linux.py` |
| `.run` | `build_linux_installer.py` | invoked by `build_linux.py` |
| validate | `validate_package.py` | invoked on extracted artifact |
| windows payload | `build_app_installer.sh` (bash/curl/unzip/sha256sum/git-bash) | pure-Python staging in `build_windows.py` — only Python + NSIS needed |
| manifests | bundle manifest | + `build-manifest.json` (commit, dirty, tool versions, artifact hashes, validation results) |

Pins: new `build-scripts/assets.json` holds URLs + versions; sha256
digests stay authoritative in `3rdparty/dagu/dagu.sha256` /
`3rdparty/caddy/caddy.sha256` for existing assets; new pins
(python-embed zip, caddy-windows zip) live in `assets.json`.
Version: `release/release.json` (single source). Output:
`dist/release/` (artifacts + SHA256SUMS + manifests + logs);
staging in `build/` (gitignored). `.nsi` files gain `!ifndef VERSION`
so `-DVERSION` overrides. `3rdparty/git` acquisition dropped — not
in `INCLUDE` (dead CI weight). NSIS is the only documented system
prerequisite (script attempts `apt`/`choco` install, else fails with
instructions).

## 1. Repository-result import

Import KRE report folders produced **outside** orchestrated runs —
i.e., result evidence that exists on disk (JUnit XML, KRE native
HTML/CSV/execution.log, screenshots, HARs) but was not generated
through a dispatched Execution.

Open questions to resolve first:

- Import unit: a report folder → what does it attach to? A synthetic
  Execution record (source=import) or a new entity (ImportedRun)?
  Decision needed against `execution.py` state model — imported runs
  have no dispatch outbox, runner, or snapshot.
- Result ingestion reuse: `results.parse_junit` +
  `aggregate_outcome`/`entry_state` already convert JUnit →
  TestResult rows for dispatched runs; the import path should reuse
  these, not fork them.
- Artifact storage: remote-runs evidence tree convention —
  imported evidence should land under an equivalent immutable store
  with its own provenance marker.
- rlabs: can an imported run feed report generation? reportingLabs
  consumes execution results+artifacts — verify adapter input
  assumptions before promising coverage.
- Authorship/audit: import is a privileged mutation — admin-gated,
  audited, timestamped, with the importing identity recorded.
- Idempotency is required (see #2): re-importing the same folder
  must not duplicate results.

## 2. Duplicate prevention

Dedup of imported/ingested results so repeated imports (and,
defensively, retried ingestion of dispatched runs) don't produce
duplicate TestResult/artifact rows.

- Natural key candidates: report-folder path + file digest (SHA256),
  or (project, suite, kre run timestamp, case name, attempt).
- Decision: dedup at artifact level (same bytes → same stored
  artifact, reference-counted or skipped) vs result level (same
  case+run+attempt → upsert). Likely both, with explicit rules.
- Backfill story for rows already imported.

## Explicitly out of scope for 2.0.1

- Windows interactive/GUI acceptance and sanitised-corpus
  parity/performance/output-size — owner-side waivers tracked in
  `docs/release-summary-2.0.0.md`, not engineering tickets here.
- pyrept adoption — still gated on the re-evaluation triggers in
  `docs/decisions/reporting-backend.md` (attachment import,
  per-attempt retry detail).
