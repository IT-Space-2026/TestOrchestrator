# 2.0.1 — deferred scope, brought forward post-2.0.0 release

Created at v2.0.0 publication. Two features deferred out of 2.0.0;

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
