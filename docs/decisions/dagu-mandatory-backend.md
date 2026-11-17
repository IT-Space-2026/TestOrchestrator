# Dagu architecture decision — APPROVED: Option B

Owner decision (2026-11-03): **Dagu is the mandatory, active orchestration
backend.** Not an optional seam; the custom worker must not independently
orchestrate a competing KRE path.

## Required execution path
portal -> durable outbox -> Dagu workflow run -> authorized runner ->
existing KRE -> collect -> local results + ReportPortal -> Git.

## Implemented wiring (`dispatch.py`)
- Every execution enqueue is `dagu-dispatch` when the endpoint is set OR
  `dagu_mandatory` is on (default True — disabling requires an explicit
  settings change).
- `_handle_event` submits to Dagu FIRST, persists `executions.dispatch_id`
  as the workflow-run identity, THEN `run_execution` acts as
  executor/reconciler under that correlation.
- Unreachable/unconfigured Dagu while mandatory -> execution FAILED with
  an actionable error; ambiguous submission is never blindly resubmitted.

## Still required before live acceptance
- Verify dagu v2.25.8 API/workflow schema (bundled binary pinned;
  `3rdparty/dagu/README_SCHEMA.md`) — client calls unverified.
- Status-sync, logs, cancellation, restart-reconciliation calls exist as
  adapter methods but have no live evidence.
- Installer: Dagu service auth/connectivity/state/startup/health/
  backup — not yet wired.
