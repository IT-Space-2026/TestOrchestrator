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
| 1 | Worker download→register→dispatch→result-return (installed 2.0.2 candidate, Linux) | E2E on installed package | pending |
| 2 | Real KRE job through packaged worker | KRE 11.7.0 @ /tmp/kre-probe | **blocked — no Katalon licence** (activation fails, exit 2; recorded as external blocker) |
| 3 | Real-corpus: suite/collection/failing/import/dedup/supplement/rlabs | real KRE outputs staged in git | pending |
| 4 | Import benchmark (history scale) | generated datasets | pending |
| 5 | Interruption/recovery scenarios | targeted tests | pending |
| 6 | Evidence lifecycle + orphan cleanup (dry-run) | code + tests | pending |
| 7 | Released 2.0.1 → 2.0.2 installed upgrade (Linux) | real package, SHA-256 verified | pending |
| 8 | One-command builds + CI incl. Linux 2.0.1→2.0.2 job | workflows | pending |
| 9 | Full test suite | pytest | pending |
| 10 | Docs + release notes | this file + docs/release-notes-2.0.2.md | pending |

Windows interactive install/GUI testing remains **deferred** — not
attempted, not claimed. Windows CI coverage (compile + installed
validation + upgrade jobs) is retained without local Windows.

## Known limitations (carried forward)

- Windows interactive GUI and real-corpus execution checks: deferred.
- Real licensed-KRE suite execution: blocked on licence.
- Non-KRE JUnit imports → `framework='unknown'`.
- KRE options gating thresholds are declared, not verified.
- Azure DevOps repo interoperability gated on an authorized repo.
