# Dagu architecture decision (resolves the orchestration discrepancy)

## Decision (2026-11-03)

**Custom durable orchestration in the portal worker is authoritative;
Dagu is an optional integration seam, not the orchestrator.** The
`dagu-dispatch` path submits a correlated run record when
`dagu_endpoint` is configured, but execution state, leases, cancellation,
and reconciliation live in the portal's durable state machine
(`executions` + `outbox_events`).

## Why this shape now

- The durable requirements (§7) — transactional execution+outbox
  persistence, ambiguity reconciliation, leases/heartbeats, process-tree
  cancel — are satisfied and tested by the worker. Delegating them to an
  unverified Dagu API version would regress proven behaviour for an
  unproven one.
- A REST client alone was never "Dagu integration"; making Dagu the
  active orchestrator requires the operator's live instance to pin and
  verify (recorded in integration-contracts.md as a hard prerequisite).

## Consequences of maintaining custom orchestration

- Portal must own retries, reconciliation, and cancellation correctness
  forever (Dagu would not retire that burden anyway for KRE subprocess
  control on runners).
- No external orchestration service is required to operate the product —
  simpler installs, but Dagu-specific features (DAG visualization,
  upstream scheduling UI) are unavailable.
- Migration path stays open: `dagu-dispatch` events already exist in the
  durable schema, and submission persists `executions.dispatch_id`, so a
  verified Dagu can later take over scheduling without data migration —
  the worker becomes its executor.

## Alternative considered (rejected for now)

Dagu as active orchestrator: requires verified API schema on the
operator's pinned version before any dispatch claim; reverting is cheap
if the live integration proves out.
