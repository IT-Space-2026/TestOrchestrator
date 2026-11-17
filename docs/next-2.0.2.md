# TestOrchestrator 2.0.2 — hardening release

Focused hardening release on top of v2.0.1. No new feature scope:
validate current worker execution end to end, measure and improve
import reliability where the evidence justifies it, preserve the
one-command builds.

Branch: `feature/2.0.2` (from `main` @ `1b84ba3`, which contains the
v2.0.1 release merge `a2db2d7` and tag commit `1f48953`).

## Scope

1. **Worker execution E2E** — close the gap between packaged-worker
   inventory checks and actual worker execution on Linux:
   build → install → download worker asset **through the installed
   app** → register → dispatch a job → verify result return, evidence,
   reporting-labs, Dagu version alignment.
2. **Linux real-corpus validation** — normal suite, collection, failing
   run, import, repeat-import dedup, late-suite supplement,
   reporting-labs; compare against recorded baselines where available.
3. **Import matching benchmark** — reproducible scale measurements;
   optimize only where material overhead is confirmed.
4. **Import interruption/recovery review** — preview/confirm
   interruption, evidence-vs-DB ordering, per-candidate failure,
   retry, concurrent confirm, missing legacy evidence.
5. **Evidence lifecycle** — ownership/deletion rules; safe orphan
   cleanup with dry-run; shared-evidence protection.
6. **Installed 2.0.1 → 2.0.2 upgrade** — real released package,
   checksum-verified.
7. **Builds/CI preserved** — one-command builds unchanged; add Linux
   2.0.1 → 2.0.2 upgrade CI job.

## Baseline review (confirmed before implementation)

### Import matching cost (`importer.py`)

`classify()` iterates **every** `Execution` in the project and calls
`fingerprint_live()` on each — for unstamped rows this reads JUnit
artifacts from disk (lazy fingerprinting), and for every non-matching
row the suite+timestamp overlap check re-reads artifacts. Cost is
O(history × per-execution disk reads) **per candidate** — material
overhead at realistic history sizes, to be measured in phase 4.

Targeted improvement (review target, not speculative):
- Indexed fast paths first: `run_uuid` (partial unique index) and
  `content_hash` (lookup index) lookups before any scan.
- Persist lazily-computed fingerprints back onto unstamped rows —
  one-time cost instead of every-import cost.
- Residual scan only where needed (unstamped rows + suite-overlap
  check for non-matching rows).

### Interruption/recovery (`importer.py::confirm_import`)

- Per-candidate `db.session.commit()` — **not** an atomic batch;
  documented as such.
- `_create_execution` flushes `ex` (id allocated), copies evidence to
  `instance/imported-evidence/<exec_id>`, then commits at the caller —
  a failure between evidence copy and commit leaves **orphan evidence
  with no DB row** (confirmed defect class; fix via lifecycle rules +
  safe cleanup).
- **Concurrent confirmation of the same candidate**: two requests can
  both read `status='pending'` and both `_create_execution`. For
  uuid-less candidates the content_hash is deliberately NOT unique →
  no IntegrityError → **duplicate executions** (confirmed defect —
  violates "confirmed duplicates create no additional results").
  Fix: atomic claim (`status pending → processing` UPDATE-WHERE), with
  crash-stale claims reclaimable.
- Staging checkout persists until confirm completes; interruption
  mid-confirm leaves `session.status='previewed'` and pending
  candidates — retryable (good).
- Missing/unreadable legacy evidence: `fingerprint_live` returns
  `("","")` silently → candidate classifies `new` → a legacy run can be
  invisibly duplicated. Honest behaviour kept, but a warning that N
  executions could not be fingerprinted is worth adding (review
  target).

### Evidence lifecycle (`gitsync.py::delete_report`)

- Execution deletion (queued `report-delete` → `delete_report`)
  removes the **published remote** report and the DB row, but never
  removes the local `report_dir` / `instance/imported-evidence/<id>` —
  **local evidence is orphaned on every execution delete** (confirmed
  defect).
- No project-deletion route exists — execution delete is the only
  lifecycle path; imported evidence is per-execution so ownership is
  `Execution.id`.
- Supplement copies additional files into an existing report_dir —
  shared-evidence rule: a report_dir may be the `report_dir` of its
  owning execution only; never delete files still referenced by an
  `Artifact` row.

## Acceptance matrix

| # | Check | Method | Status |
|---|-------|--------|--------|
| 1 | Worker download→register→dispatch→result-return (installed 2.0.2 candidate, Linux) | E2E on installed package | **PASS** (exec `2b044561`, completed/passed, rlabs ready) |
| 2 | Real KRE job through packaged worker | KRE 11.7.0 @ /tmp/kre-probe | **blocked — no Katalon licence** (real dispatch executed exec `74af175f`: activation failed, exit 2, worker log returned, classified infra-failure) |
| 3 | Real-corpus: suite/collection/failing/import/dedup/supplement/rlabs | real Katalon 11.7.0 result staged in git | **PASS** — details below |
| 4 | Import benchmark (history scale) | generated datasets | **DONE** — material overhead confirmed + fixed |
| 5 | Interruption/recovery scenarios | targeted tests | **DONE** — 2 defects fixed + covered |
| 6 | Evidence lifecycle + orphan cleanup (dry-run) | code + tests | **DONE** |
| 7 | Released 2.0.1 → 2.0.2 installed upgrade (Linux) | real package, SHA-256 verified | **PASS** |
| 8 | One-command builds + CI incl. Linux 2.0.1→2.0.2 job | workflows | pending CI |
| 9 | Full test suite | pytest | **631 passed, 1 skipped** |
| 10 | Docs + release notes | this file + docs/release-notes-2.0.2.md | in progress |

## Results

### 1. Worker E2E on the installed 2.0.2 candidate (Linux)

Driver: `packaging/ci/worker_e2e.py` run against the generated package
— no source-checkout shortcuts.

**Final-candidate run (release candidate `4966330`)** — package
`testorchestrator-2.0.2-linux-x86_64.tar.gz`, 278,810,011 B,
SHA-256 `8ef43a80582fe0d5eddbcd117e5b855d463edb646a494b3f02c3fc6ab8a52380`,
installed to `/tmp/to202-rc`:

- Worker asset downloaded through the installed app's authenticated
  route `/system/infrastructure/dagu-worker/dagu_2.27.0_linux_amd64.tar.gz`:
  64,492,882 B, SHA-256 `a9045768c9777d0b938585696e0491bc0ce46fbeb0419989b34fde63a0cc6f85`
  — matches the pinned digest exactly.
- Coordinator `start-all` (http :8083, gRPC :53055); downloaded worker
  binary `version` → **2.27.0** = coordinator pin. Registered as
  `e2e-worker-202` with label `kre=e2e-202`; coordinator reported it
  via `/api/v1/workers`; dispatched with `worker_selector=map[kre:e2e-202]`.
- Execution `20c0ca6f8eba432195e6e6d03e68de99` — **fixture-KRE path**:
  a script emitting genuine Katalon-format JUnit/log/screenshot stood
  in for the KRE binary. Demonstrated coverage: application-served
  worker download, registration, selector-based dispatch, task
  execution, result/evidence return, and reportingLabs generation
  (`completed`/`passed`, 3 cases, `rl_status=ready`). This is
  pipeline coverage — not licensed KRE execution.

**Earlier runs (supporting evidence, labelled by commit):**

- `2b0445614974482d9d9e5789cbd8852d` — same fixture-KRE path, on the
  package built from `bc600a5` (278,795,546 B, `6141cc2c…`). Passed.
- `74af175f7e9e41a99f064b1338531648` — **real KRE 11.7.0** on the
  `bc600a5` package: real `katalonc` launched on the worker, reached
  activation, failed (`valid offline licenses: 0 … Activation
  failed`, KRE exit 2), failure logs returned portal-side, classified
  `failed`/`incomplete` — never falsely green. **Licensed test
  execution remains blocked on a Katalon licence/apiKey this host
  does not have.** No relevant code changed since — the real-KRE run
  was not repeated.

### 2. Real-corpus validation (Katalon-cert, KRE 11.7.0 outputs)

Dataset: real Katalon project `Katalon-cert` + a genuine KRE 11.7.0
result (`Reports/20261110_122651`, suite API-TS, 2 cases, uuid
`55e8063a-…`, plus its collection-layout mirror) staged in git —
`Reports/` is gitignored by default so it was force-added (import
reads repo content).

| Scenario | Result |
|---|---|
| Historical import (standalone layout) | new → exec `b6d35526`, completed/passed, 2 cases, run_uuid `55e8063a` |
| Repeat import | preview `duplicate` → confirm `skipped`, no extra rows |
| Collection layout (same uuid) | `supplement` — merged into same execution, +1 artifact |
| Failing run (uuid `a1b2c3d4`, API1 FAILED) | new → exec `3715d496`, `completed`/`failed`, API1 failed + API2 passed |
| Late-suite supplement (IT-Space2 arrives later, same uuid) | `supplement` — +2 test results, +1 artifact, suite_keys unioned |
| ReportingLabs on imported exec | `ready`, `index.html` (818 KB) + `report.json` + evidence assets (HARs, logs) attached |

Output sizes: imported execution evidence 2.7 MB (incl. rlabs gen);
remote-run evidence 1.3 MB. No published baseline exists for these
exact runs — sizes recorded here as the 2.0.2 reference.

Commit labels: the table above ran on the `bc600a5`/`e0b65ca`
packages; the late-suite suite_keys union was **re-verified on the
release candidate** (`4966330`, installed `/tmp/to202-rc`) —
supplement filled `suite_keys` →
`[["Test Suites/API-TS",…],["Test Suites/IT-Space2",…]]` — and the
evidence-prune dry-run listed only the true orphan while the
referenced evidence dir stayed intact.

Live normal-suite/collection/failing *execution* coverage comes from
the worker E2E above (fixture-KRE pass + real-KRE licence-blocked
run); KRE licence blocks a full licensed live corpus.

### 3. Import benchmark (SQLite, Dell-7020, warm FS cache)

Instrument definitions (from `scripts/benchmark_import.py`):
- **Query** — one SQL statement observed via SQLAlchemy's
  `before_cursor_execute` event on the app engine.
- **Evidence read** — one call to `importer._looks_junit`, i.e. one
  JUnit file opened/sniffed for parsing.
- **History** — generated executions with real JUnit dirs on disk.
  `stamped` = `run_uuid`+`content_hash` populated (2.0.1 already did
  this); `suite_keys_json` is only populated by 2.0.2, so **all**
  pre-existing rows pay a one-time lazy fingerprint on the first
  import after upgrade regardless of stamped state.

Measured on the current code (worst case: a no-uuid candidate):

| History | First preview (backfill) | Confirm | Subsequent previews |
|---|---|---|---|
| 100 | 0.10 s / 249 q / 102 r | 28 q / 1 r | 0.008 s / 9 q / 2 r |
| 400 | 0.35 s / 969 q / 402 r | 28 q / 1 r | 0.006 s / 9 q / 2 r |
| 800 | 0.72 s / 1929 q / 802 r | 22–28 q / 1 r | 0.007 s / 9 q / 2 r |

Baseline for comparison (2.0.1 code, measured earlier): the *same*
~1 read + ~1.2 queries per history row recurred on **every** import —
967 q / 962 r at n=800 repeatedly, not just once. The 2.0.2 change
converts the linear scan into a **one-time** lazy-fingerprint cost
paid on the first import per row (persisted `suite_keys_json`,
`[]` marker for unfingerprintable rows); every import after that is
flat (~9 queries, ~2 reads — the candidate's own JUnit parse plus
bounded residual checks).

Why "4 reads at 800 vs 1 at 100" in the earlier snapshot: reads are
the candidate's own parse (1) plus rows still lacking usable keys at
that moment; the count is bounded and non-scaling — the numbers above
are the cleaner per-mode split.

Changes: `run_uuid`/`content_hash` indexed fast paths, persisted
`suite_keys_json` (migration `r8s9t0u1v2w3`), lazy-fingerprint
persistence, residual scan only for never-fingerprinted rows.
Correctness preserved: content hash is evidence not identity;
distinct uuids stay distinct; ambiguous inputs still require review.

Reproduce: `.venv/bin/python scripts/benchmark_import.py
--sizes 100 400 800 [--unstamped 0.2]`.

### 4. Import interruption/recovery — defects found & fixed

- **Concurrent confirm race** — two confirmations of the same
  candidate could both pass `status='pending'` and create duplicate
  executions (content_hash is deliberately non-unique → no DB
  backstop). Fixed by `ImportLock` (per-project single-writer claim
  with stale-claim reclaim) + transactional recheck.
- **Failed candidates unretryable** — `_confirm_import_inner`
  unconditionally deleted staging, so a failed candidate could never
  be retried ("staging checkout expired"). Fixed: staging is removed
  only when no retryable candidates remain.
- **Unstamped/unfingerprintable history** — lazy fingerprinting could
  silently fail; rows now persist `suite_keys_json='[]'` (fingerprinted,
  nothing found) and the import reports a warning count instead of
  rescanning forever.
- **Late-suite suite_keys loss** — `_supplement` only wrote
  `suite_keys_json` when NULL; a late collection suite's key never
  reached the index. Fixed to union (found during real-corpus
  validation, commit `e0b65ca`).
- Per-candidate transactions documented: a batch is NOT atomic —
  committed candidates stay committed; pending/failed stay retryable.

### 5. Evidence lifecycle

- New `testorchestrator/evidence.py`: ownership rules (evidence dir is
  owned by exactly one `Execution.id`), managed-root confinement
  (`imported-evidence/`, `remote-runs/`, `executions/`), traversal and
  symlink-escape rejection, shared-evidence protection (never delete a
  file another `Artifact` row references).
- `delete_report` now removes the local `report_dir` +
  `imported-evidence/<id>` **after** the DB delete commits (was:
  orphaned on every delete).
- CLI `toctl evidence-prune [--apply]` — dry-run orphan listing by
  default; `--apply` deletes only managed, unreferenced dirs.

### 6. Released 2.0.1 → 2.0.2 upgrade (Linux, isolated)

- Source package: real released
  `testorchestrator-2.0.1-linux-x86_64.tar.gz`, 278,783,010 B, SHA-256
  `52efcda5ae812e06604fc3336499d4fc7afb63557449cf5c6efdd6c49804d488` —
  matches the published release record.
- Seeded under 2.0.1: admin user, settings, git binding, imported
  real-corpus executions with evidence + rlabs `ready`, unstamped
  legacy executions.
- Upgrade: `db-upgrade` applied exactly one migration
  (`q7r8s9t0u1v2 → r8s9t0u1v2w3`) on the real 2.0.1 DB.
- Verified: settings + setup flag retained; executions + results +
  evidence + rlabs/Katalon reports accessible; repeat import →
  `duplicate`/`skipped`; new import → `imported`; legacy rows lazily
  stamped; persistence across separate process opens.
- Commit labels: first run on the `e0b65ca` build; **re-verified on
  the release candidate** `4966330` (fresh 2.0.1 instance seeded with
  imported exec `87a90a16` + rlabs `ready` + legacy row `ae71accf`,
  migration applied, retention/dedup/new-import all confirmed).

### 7. Confirmed defects fixed in 2.0.2

1. No-uuid import rescanned project history linearly (O(N) evidence
   reads + queries) — indexed fast paths + persisted fingerprints.
2. Concurrent confirm → possible duplicate executions — `ImportLock`.
3. Failed candidates unretryable (staging deleted unconditionally).
4. Execution delete orphaned local report/evidence dirs —
   ownership-aware cleanup.
5. Unfingerprintable rows rescanned forever — persisted `[]` marker +
   warning.
6. Late-suite supplement dropped the new suite's key from
   `suite_keys_json` — union fix (`e0b65ca`).

No migration beyond `r8s9t0u1v2w3` was needed.

Windows interactive install/GUI testing remains **deferred** — not
attempted, not claimed. Windows CI coverage (compile + installed
validation + upgrade jobs) is retained without local Windows.

## Known limitations (carried forward)

- Windows interactive GUI and real-corpus execution checks: deferred.
- Real licensed-KRE suite execution: blocked on licence.
- Non-KRE JUnit imports → `framework='unknown'`.

Pre-existing backlog (not introduced or touched by 2.0.2):
- **KRE options gating thresholds** — the `retry`, `retryStrategy`,
  and `maxFailedTests` KRE-console flags carry declared minimum-
  version requirements; they have never been verified against a
  licensed KRE build (README:493–500). Unchanged since 2.0.0.
- **Azure DevOps repo interoperability** — HTTPS/proxy/custom-CA and
  ADO git endpoints are untested because no authorized ADO repository
  has been available (README:319; runbook-authorized-host.md:60
  marks it "separate gate"). Unchanged since 2.0.0.
