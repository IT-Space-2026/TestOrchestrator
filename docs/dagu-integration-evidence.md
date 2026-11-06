# Integrated fixture run — live evidence (2026-11-03)

> Bundled pin updated to 2.25.8 (2026-11-06): contract tests re-run
> green against the new archive (submit/auth/stop paths unchanged);
> dated records below remain as demonstrated on 2.25.2.

**Label: live Dagu/agent FIXTURE execution.** The chain below ran for real
against bundled Dagu 2.25.2, but the executable was a bash fixture — KRE
CLI compatibility and the A01 real-KRE gate remain **unverified**.

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

## Closure status (milestone: Dagu/fixture integration)
- Success demo run: dagRunId `034r6dpMcLxcMTkFseVGMx` -> COMPLETED/passed.
- Cancel demo: agent-path CANCELED/incomplete + tree kill (documented).
- Reconcile tests: 4 pass (UNKNOWN never re-dispatches; RUNNING and
  CANCEL_REQUESTED expired-lease -> UNKNOWN; terminal-result guard).
- Contract test tests/contract/test_dagu_contract.py PASSES vs bundled
  2.25.8 (self-starts, submit->dagRunId->status->CLI stop->aborted);
  DAGU_COORDINATOR_ENABLED=false documented for multi-instance.
- Token audit: no token material in dagu run data/logs; delivery only
  via 0600 agent.toml.
- scripts/provision_dagu.py: DAGU_HOME + protected creds + workflow
  placement + systemd unit + health pointer (scaffold; root/service
  activation needs target host).
MILESTONE: Dagu/fixture integration = COMPLETE at fixture level.
Remaining gates: real KRE, live ReportPortal attachments, second runner
host, full-stack target-OS install.

## Option B completion — remote monitoring + reconciliation (2026-11-03)

- The DAG step is now `--once --monitor`: the step executes a locally
  assigned task if present, then polls `/api/agent/state/<id>` until a
  terminal outcome or explicit uncertainty (`unknown`/timeout → step
  fails). The dagu run mirrors the run's fate; it no longer finishes
  after dispatch. Local-runner tokens are the orchestration credential
  that may read any execution's state label; foreign remote runners get
  404.
- `reconcile_dagu_runs` is wired into `flask worker` cycles (skipped
  when Dagu unconfigured/unreachable). A *running* dagu run no longer
  extends the execution lease — heartbeats own renewal, so a dead agent
  still reconciles to UNKNOWN within ~60 s.
- Sanitization: runtime `credential_ref` resolves to the invocation
  credential only inside the scoped task; the value reaches argv as
  `-apiKey=…` (KRE activation form) while `run_invocation` redacts it
  from `kre-invocation.log` AND post-scrubs captured stdout/stderr.
- Dagu auth corrected to `auth.mode: basic` (verified vs bundled 2.25.2);
  `provision_dagu.py` emits basic config + 0600 credential file.

### Live fixture demos (same session)

| Scenario | Execution | Result | Dagu run |
|---|---|---|---|
| remote success/failure | `27bf2d75` (runner rem-1, daemon) | `COMPLETED` / `failed` (1 pass + 1 fail collected) | `034r9iwAhJKmkauieuLTMm` succeeded |
| agent disconnect mid-run | `881923bd` (daemon killed during 300 s fixture) | `UNKNOWN`, "lease expired; state uncertain" — never re-run | `034r9jhhuWZbSpqLxmxbtA` failed |
| local mode (earlier run) | `5fb35a1f` (local-1, Dagu-launched agent) | `COMPLETED` / `failed`, profile/browser argv verified | `034r8xn1yT3TsouFs8mmJA` succeeded |
| cancellation | covered by tests (cancel poll → process-tree kill → `canceled`) | — | — |

### Regression coverage added

- `tests/contract/test_dagu_provisioning.py`: provision → boot bundled
  2.25.8 → 401 without/with wrong creds → basic-auth health + dags +
  workflow presence — no mocks.
- `tests/test_runbook_wiring.py`: probe-only smoke, explicit `kre-exec`
  suite, `dagu-config`, token issuance, state-endpoint scoping,
  reconcile-wired worker (running dagu never masks disconnect),
  secret-bearing-arg scrub across logs/artifacts/audit/UI/Git export,
  full runbook fixture path e2e.
