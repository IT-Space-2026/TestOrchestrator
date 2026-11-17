# 2.0.1 — deferred scope, brought forward post-2.0.0 release

Created at v2.0.0 publication. Two features deferred out of 2.0.0;
implementation on branch `feature/2.0.1` (from tag `v2.0.0`/`cdc4a91`).

## Implementation plan (updated during build)

### A. Repository-result import — design (post-inspection)

Existing surface reused, never forked: `Execution`/`TestResult`/
`Artifact` (already framework-agnostic), `results.parse_junit`,
`aggregate_outcome`, `build_evidence_manifest`, artifact serving
routes, and the reporting-labs adapter (needs only `ex.results` +
`ex.report_dir` — works unchanged for imported runs).

**Inspected real inputs** (live remote-run evidence):
`Reports/<YYYYMMDD_HHMMSS>/[<Suite>/]<ts>/` containing
`JUnit_Report.xml` (testsuite `@id`, `@timestamp`, `@hostname`,
properties: katalonVersion, projectName, os), `execution0.log`,
`console0.log`, `execution.properties`, `execution.uuid` (KRE run
identity — the primary dedup key), `testCaseBinding`, `<ts>.html`,
`<ts>.csv`, `tsc_id.txt`, `requests/` (response captures).

- **Supported inputs**: a run directory = any directory containing
  ≥1 parseable JUnit XML (`JUnit_Report.xml`, else any `*.xml` that
  parses via `parse_junit`). Everything else in the dir is evidence.
  HTML/CSV/properties alone NEVER reconstruct a run — a dir without
  parseable JUnit is an `unsupported`/`malformed` candidate, never an
  execution. Generic (non-Katalon) JUnit accepted with reduced
  metadata — documented as best-effort.
- **Grouping**: one run dir → one execution. A multi-suite Katalon
  collection produces one dir per suite → separate executions (KRE
  writes separate JUnit per suite; suite identity is preserved).
- **Identifiers & dedup** (project-scoped, evaluated in order):
  1. `run_uuid` — `execution.uuid` content, exact match → **duplicate**
     (skip, link to existing).
  2. `content_hash` — sha256 over canonical
     `{suite_key, timestamp, [classname,name,status,duration_s], totals}`
     exact match → **duplicate** (covers copied/renamed dirs; a rerun
     has a different timestamp → different hash → retained as new).
  3. same `suite_key`+`timestamp` but a strict subset/superset of
     cases → **ambiguous** — requires explicit "import as new"
     review, never silently merged or created.
  4. uuid/hash match where the existing row is missing data the input
     demonstrates (unfinished_at, missing artifacts) → **supplement** —
     fills only empty fields, existing values/provenance preserved.
  5. everything else → **new**.
  Live executions get `run_uuid`/`content_hash` stamped at `_normalize`
  (same canonicalisation); historical live rows are fingerprinted
  lazily from their `report_dir` at match time. Constraint-backed:
  partial unique indexes on `(project_id, run_uuid)` and
  `(project_id, content_hash)` make concurrent imports transactional —
  a race between preview and confirm resolves via recheck +
  IntegrityError→duplicate, never a second row.
- **Schema** (migration `q7r8s9t0u1v2`, additive):
  `executions.origin` (live|imported), `.framework` (default
  `katalon`), `.imported_at`, `.imported_by`, `.run_uuid`,
  `.content_hash`, `.source_repo`, `.source_commit`, `.source_path`;
  new `import_sessions` (durable preview: binding, requested rev,
  resolved sha, result dir, user, status, summary) and
  `import_candidates` (per-detected-run decision, fingerprints,
  warnings, match, outcome) — preview persists bookkeeping only,
  never history rows.
- **Timestamps**: `started_at`/`finished_at` = original JUnit
  timestamps; `created_at` = import time (history ordering is honest:
  the row appeared when imported); `imported_at`/`imported_by` audit.
- **Missing data**: suite binding best-effort (link by suite identity
  when a project suite matches, else NULL + name in snapshot);
  runner/runtime/environment stay NULL — never invented.
- **rlabs**: generated via the existing route; imported executions
  carry results + evidence so generation works; insufficient data
  yields the adapter's honest `missing`/`failed` statuses.
- **Safety**: staging = isolated checkout (`git_backend.checkout` at
  resolved sha into `instance/import-staging/<session>/`); safe_relpath
  on every artifact; symlinks/traversal rejected; file-count/size/XML
  limits; evidence copied to `instance/imported-evidence/<exec_id>/`
  (durable, outside the transient checkout); ElementTree-only XML
  (no DTD/entities — same as `parse_junit`).

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

## Implementation record — repository-result import (deliverable 1)

**Status:** implemented on `feature/2.0.1`, all automated checks green.

### What was built

- `testorchestrator/importer.py` — detection → parse → fingerprint →
  classify → preview → transactional confirm. Parsers are separate
  from repository access and dispatch.
- Schema (additive, migration `q7r8s9t0u1v2`):
  `executions` gains `origin`, `framework`, `run_uuid`,
  `content_hash`, `imported_at`, `imported_by`, `source_repo`,
  `source_commit`, `source_path`; new bookkeeping tables
  `import_sessions`/`import_candidates` (workflow state only — never
  history records). Partial unique indexes on
  `(project_id, run_uuid)` and `(project_id, content_hash)` make
  concurrent imports transactional.
- `results.parse_junit` now exposes per-`<testsuite>` metadata
  (`suites`) used by fingerprints; live normalization stamps
  `run_uuid`/`content_hash` so later imports match live runs.
- Portal: `GET/POST /projects/<id>/imports/new|preview`,
  `GET /projects/<id>/imports/<sid>`,
  `POST .../confirm` — `EDIT_ROLES`, project-scoped via
  `_project_or_404`. History gains `origin` and `framework` filters
  and an `imported` badge; detail view shows provenance/warnings and
  hides Cancel/Retry for imported runs.

### Identity & dedup rules (as designed, with one real-data fix)

- `execution.uuid` (KRE run id) + canonical `content_hash` over
  suite keys, timestamps, per-case tuples, totals.
- **Collection runs**: KRE emits one report dir per suite sharing a
  single `execution.uuid` — dirs with the same uuid merge into ONE
  candidate/execution (discovered on real E2E evidence; member dirs
  recorded on the candidate for confirm-time re-parse).
- Confirmed duplicate → skipped + linked. Same-run-with-gaps →
  `supplement` fills only demonstrably missing fields (timestamps,
  unseen evidence by SHA-256) preserving provenance. Same suite+time
  but differing case set → `ambiguous`, requires explicit
  "import as new". Different timestamp = different run, even with
  identical outcomes. Filenames/commits/totals alone never identify
  a run.
- Confirm re-parses from staging and rechecks inside the per-candidate
  transaction; IntegrityError races resolve to the existing row.
- Evidence copied to `instance/imported-evidence/<exec_id>/`
  preserving repo-relative layout; staging under
  `instance/import-staging/` cleaned after confirm (7-day lazy sweep).
- Limits: 20k dirs scanned, 2k files/512MB per run, 64MB per XML,
  500 candidates; symlinks and dotdirs skipped; `..` paths rejected.

### Verification

- 19 new tests (`tests/test_import_results.py`): KRE parse, multi-dir
  grouping, duplicates, renamed copies, distinct reruns, ambiguous →
  review → force-new, legacy live-exec match, supplement, malformed/
  unsupported, project-scoped dedup, no dup test/attempt rows,
  preview→confirm race, auth/roles, history/detail visibility, reopen
  persistence, rlabs generation.
- Full suite: **610 passed, 1 skipped**.
- Migration verified: v2.0.0-shape DB → `alembic upgrade head` adds
  all columns/tables/indexes.
- **Real-input acceptance** (actual KRE output from the recorded 2.0.0
  E2E evidence, staged into a git repo — real artifacts, not
  synthetic): preview detected 3 real runs (single-suite, 2-case
  collection merged into one 26-case execution, distinct rerun);
  import produced correct results/artifacts; reportingLabs `ready`
  for all three; repeat import → all duplicates; a legacy unstamped
  live execution was matched via lazy fingerprint (supplement, no
  second row); persisted across app reopen.

### Known limitations (honest, not passes)

- Matching scans candidate executions per project — fine at current
  scale; index-backed lookup can be added if needed.
- Generic JUnit XML (non-KRE) is detected and parsed, but framework
  attribution stays `katalon` until additional parsers register.
- Windows interactive + real-corpus checks remain waived as in 2.0.0.
