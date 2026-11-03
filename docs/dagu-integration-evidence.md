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
