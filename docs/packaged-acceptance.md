# Final packaged acceptance procedure — 2.6.0

Runs only after all implementation milestones and the Windows worker
matrix pass on the **packaged** build. Synced-working-tree evidence
(the `/tmp/to251d` runs throughout `acceptance-2.6.0.md`) is
development evidence — this procedure is the only source of
clean-package claims. Every artifact is SHA-256'd and every run
records the exact commit + `deployed_rev`.

## 1. Build

1. Clean checkout of the release commit; `git status` empty;
   record `git rev-parse HEAD`.
2. Build all packages (Linux bundle, Linux worker script, Windows
   controller installer, Windows worker installer) via
   `build-scripts/` per `docs/build.md`.
3. Record every artifact name + SHA-256 in the ledger.
4. Install on **clean** hosts/VMs — no reused data dir, no reused
   worker. Existing-data behavior is covered separately by the
   upgrade row (§4).

## 2. Eight-combination smoke matrix

The agreed matrix is **framework × execution target** — 4 frameworks
× (Linux worker, Linux SSH, Windows worker) = 8 required cells.
Controller operating-system support is a SEPARATE dimension covered
by §4/§5 (install + upgrade on each controller package), not by this
matrix — describing it as a controller-OS matrix would claim
coverage it does not test.

| Framework | Linux PB worker | Linux SSH target | Windows PB worker |
|---|---|---|---|
| Playwright | required | required | not newly claimed |
| Cypress | required | required | not newly claimed |
| Robot Framework | required | required | **required** |
| pytest | required | required | **required** |

"Not newly claimed" = Playwright/Cypress on Windows were already
exercised on the Windows worker path in earlier acceptance and are
not re-gated here; Robot+pytest on Windows are the release-critical
new claims and map to kit rows W1–W4.

Each cell: install → register/probe → run one fast suite → verify
COMPLETED + normalized results + sealed manifest + artifact
downloads + `deployed_rev` attribution. A cell blocked by an
unavailable host is recorded **BLOCKED**, never silently dropped —
the matrix is accepted only when all 8 required cells are PASS or an
explicit waiver is recorded in the ledger.

## 3. Parallel + recovery re-verification on the package

Re-run the Stage-5 core on packaged builds (not the dev tree):

- Two concurrent executions on one runner — slot-builders both
  claimed, overlap proven by build timestamps.
- pytest-xdist / Playwright-workers / Pabot parallel runs.
- Parent/child: mixed pass+fail aggregation; cancel running +
  queued child (queued child never launches).
- Worker PB loss mid-run: attempt 2 counted + env-verified orphan
  resolution + `TO_RETRY_BLOCKED` path where applicable; master
  restart; SSH-session loss — all three distinct.
- `retry_enabled=false` + `max_attempts=1`: the resubmit is blocked,
  execution → UNKNOWN on the reconciliation queue.
- Windows worker PB loss: the resubmitted build is BLOCKED by the
  PowerShell prep counter (`TO_RETRY_BLOCKED`, exit 70) — no second
  CLI launch on the worker; execution → UNKNOWN (kit row W6).
- Paths containing spaces: at least one Windows run under a
  workspace/workdir with spaces (`C:\TO Work\…`) completes.
- Service-account readiness: the Windows worker service runs under
  its service account and a run completes without interactive
  login.

## 4. Upgrade + restore on packages

- Install 2.5.1 package, seed executions/history/schedules/secrets,
  upgrade in place to 2.6.0 (`toctl db-upgrade` runs at service
  start): all rows preserved, `notified_at` backfilled, boot clean.
- Membership after upgrade: users with prior explicit
  `allowed_projects` keep them; previously-unrestricted users are
  flagged `none — needs assignment` on the Users page (the review
  workflow) — no silent mass-grant.
- `toctl backup` → `recover` to a scratch dir: member count, table
  counts equal, symlink members restore, instance boots.
- Restore preserves assignments: restore a backup taken after
  configuring memberships; verify each user's `allowed_projects`
  survives and enforcement still applies.
- `flask diag` (deployed-revision) run pre-acceptance to prove the
  packaged tree is what the services import.

## 5. Security regression on the package

- Report serving sandboxed; manifest paths root-contained.
- Secret material: nothing plaintext in snapshots/audits/logs;
  API tokens SHA-256'd; webhook HMAC via SecretRef.
- CSRF: all mutating portal routes covered; `/api/v1` bearer-exempt
  only.
- RBAC: project scoping + role gates on the new surfaces
  (reconciliation, matrix, API tokens, webhooks).
- Project membership on the package: two projects × two same-role
  users — list/detail/artifact/API isolation, identifier
  substitution denied, revocation effective without logout,
  admin-retains-all, unassigned user sees the empty state.

## 6. Exit criteria

- All 8 matrix cells PASS (or waived with reason in the ledger).
- Windows W-matrix complete (windows-acceptance-kit.md).
- Full unit suite green on the release commit (result recorded
  separately from earlier runs).
- Every acceptance row carries: commit, package SHA-256,
  execution/build ids, deployed_rev.
- `docs/acceptance-2.6.0.md` updated; PR remains **draft** until
  human sign-off. Passing this procedure does not authorize merge.
