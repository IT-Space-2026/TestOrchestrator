# Operator runbook: first real KRE execution (A01 gate)

## Prerequisites (you provide, we never install/license/upgrade)
1. Installed, licensed KRE binary, e.g. `/opt/katalon/katalonc`
2. Working browsers/drivers for your project
3. A Katalon project checked out where the runner can read it
4. A runner host able to launch that binary (or this host for local mode)
5. Portal reachable (https://localhost) + Admin login

## Settings to configure (System -> Infrastructure)
| Field | Value | Notes |
|---|---|---|
| Runner | name + connection_mode `local` (this host) or `remote` (agent) | remote needs issued agent token on that host |
| RuntimeConnection | executable_path = your real katalonc path, workspace_root = a scratch dir | "Test connection" runs `-version` only |
| dagu_endpoint | your Dagu URL | mandatory orchestrator (Option B) |
| git_binding / suite | clone/index your project, build a suite | TestManager/Admin |

## Smoke sequence
```bash
# 1. Probe the binary (no licence action):
flask kre-smoke /opt/katalon/katalonc --project /path/to/project
# expect: "probe: detected version X.Y.Z", exit=0, reports discovered []

# 2. In the portal: register RuntimeConnection with the same path ->
#    Test connection -> should report version + writable workspace.

# 3. Dispatch one suite on that runner (portal -> outbox -> Dagu ->
#    agent/local -> KRE console invocation).
```

## Expected outputs (sanitized evidence to return)
- `Execution.id`, `dagu dagRunId`, runner name, `snapshot` hash
- KRE stdout/stderr captured under the attempt `logs/`
- `Results` row per test case (status/attempt/iteration)
- Artifacts: junit + logs (+ screenshots if produced) with sha256
- Final `state`/`outcome` and `rp_state`/`git_state` fields
- For a failing test: collected anyway, outcome=failed

## Explicitly NOT provided here
Licence activation, KRE updates, ReportPortal credentials, remote-host
firewall rules — all operator-owned.
