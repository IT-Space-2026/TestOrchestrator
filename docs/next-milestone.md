# Next milestone — completed workflows vs remaining limits

## Completed user workflows (this build)
- Fresh install → Admin/Admin → mandatory strong change → dashboard; `flask set-admin-password <password>` (headless positional arg; password appears in argv).
- Admin: users CRUD + reset (session revoke), credentials store, runners/runtimes register + probe + drain, network/ports/certs with staged apply + `flask caddy-rollback`.
- All roles: About dialog, dashboard links, notices, project/execution/report viewing (project-scoped).
- Manager/Admin: project + git binding create/sync, suite create from binding cases, plan create + add suite entries, release create + mark-released, suite duplicate (imported stays read-only), suite entry add (bumps version).
- Run: suite/plan run → queued → dispatched → KRE adapter (fixture-proven) → collect+normalize → local results/evidence/log pages → git export/push.

## Demonstrated failure-recovery behaviours
- Cancel → process-group tree kill → CANCELED (test_pipeline).
- Lease expiry → UNKNOWN, never re-run (test_pipeline).
- Caddy apply failure → config untouched; CLI+UI rollback.
- Publish failure with partial remote state → AMBIGUOUS (test_reportportal); clean failure → FAILED.
- Git push conflict → merge abort, remote untouched (test_gitsync_remote).
- Admin reset → live session revoked (test_infra).
- Update staging: signature/digest verify, lock, backup, checkpoint (updater.py); drain/activate/health/recover functions added, CLI wiring thin.

## Remaining limitations (not blocked by credentials)
- Rich suite/plan builder UI (ordering UX, data-binding/hook preservation, plan parallelism>1 scheduling) — partial.
- Updater: drain/migration-preflight/activation/restart/health-chain/resume exist as functions but lack an end-to-end tested apply pipeline and service integration.
- Installer: portal payload only; complete bundled stack (venv, deps, ReportPortal stack, WSL/containers) + Windows/SLES implementation missing — separate from platform validation.
- Remote transport: API tested in-process; not run on a real second host.
- Dagu: Option A/B awaiting owner approval (docs/dagu-decision.md).
- file:// Git tests are LOCAL Git integration; HTTPS/SSH creds, interruptions, provider behaviour are separate gates.
