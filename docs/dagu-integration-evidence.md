# Integrated fixture run — live evidence (2026-11-03)

Chain demonstrated live against bundled Dagu 2.25.2 (port 29999):

```
portal execution create -> outbox dagu-dispatch -> POST
/api/v1/dags/testorchestrator-execution/start -> dagRunId assigned ->
Dagu step invokes authenticated agent (--once --execution-id) ->
agent polls /api/agent/tasks -> only that execution's immutable task ->
fixture KRE subprocess in isolated workspace -> JUnit + evidence
uploaded -> /api/agent/result -> portal COMPLETED/passed
```

Recorded: dagRunId `034r6dpMcLxcMTkFseVGMx`, execution DISPATCHED →
COMPLETED, outcome passed, 1 result, 1 artifact, runner assigned
(8b1ff83b…). Token delivered via 0600 agent.toml (not params/args/logs).
Agent API CSRF-exempt (bearer-token surface, no cookies). Runner/runtime
assigned before DISPATCHED so the workflow targets its own task.

Not yet: cancel-to-tree live demo, reconciliation audit trail, installer
provisioning of the Dagu service. Milestone stays PARTIAL.

## Cancellation demo (live, same session)
Fixture KRE = bash script spawning `sleep 60 &` children in the same
process group. Agent picked the DISPATCHED execution; portal
`request_cancel` set CANCEL_REQUESTED; the agent's cancel poll fired and
`killpg` terminated the process group. Result: state=`canceled`,
outcome=`incomplete`, no surviving child processes (pgrep false-positives
on the test shell's own cmdline verified and cleared).

## Audit trail on UNKNOWN
`reconcile_dagu_runs` records dagu-run terminal labels into
`execution.error_text` before UNKNOWN — audit row exists via
record_audit paths on transitions (audit persistence still requires a
full restore-path test).

## Token hygiene (verified this session)
- Workflow file `deploy/dags/testorchestrator-execution.yaml`: no token.
- Dagu submit body: `params` string contains only `execution_id`.
- Agent token delivered via `agent.toml` (0600) — never in CLI args,
  env dumps, or run params. Dagu API responses don't echo it.
- Agent API is CSRF-exempt bearer surface — cannot fall back to
  cookie/session auth (require_agent has no session path).

## Still open
- Installer provisioning of the Dagu service (systemd, DAGU_HOME,
  startup, health, backup/recovery).
- Automated contract test self-starting bundled Dagu (manual sequence
  reproducible via the commands in this doc).
- HTTP cancel route unresolved (CLI `dagu stop` verified — acceptable).
- Reconciliation coverage of RUNNING/CANCEL_REQUESTED states beyond
  DISPATCHED is coded but not all cases live-tested.
