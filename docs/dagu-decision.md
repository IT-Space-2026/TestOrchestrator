# Dagu architecture decision — AWAITING OWNER APPROVAL

Status: **proposal pending**. The portal's durable worker is the active
orchestrator today; Dagu is an unexercised REST client. Two options are
presented for explicit owner decision — neither is claimed proven.

## Option A — permanent custom-worker orchestration (Dagu removed from mandatory scope)

- Durable requirements (§7) are already satisfied and tested by the worker:
  transactional execution+outbox persistence, lease/heartbeat
  reconciliation, process-tree cancel, ambiguous-state handling.
- Consequences: no external orchestration dependency; portal permanently
  owns scheduling/retries/reconciliation correctness; Dagu-specific UI
  (DAG visualization, upstream scheduling) is never available; the
  `dagu-dispatch`/`dagu_endpoint` code paths are deleted, not dormant.

## Option B — active Dagu integration

- Dagu submits/schedules runs; the worker becomes the executor tier.
  Requires, before any activation claim: pinned operator Dagu version,
  verified API schema (`health()`), and live acceptance evidence.
- Consequences: extra service to install/monitor/secure; two sources of
  scheduling truth to reconcile; KRE subprocess control on runners stays
  portal/agent-owned regardless.

## Explicitly NOT claimed

- Future scheduling compatibility is unproven — `dagu-dispatch` events
  and `dispatch_id` fields exist in the schema but have never been
  exercised end-to-end; adopting Option B may still require design work.
