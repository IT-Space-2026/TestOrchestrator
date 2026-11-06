# Disconnect verification plan — corrected semantics (FOR APPROVAL)

Distinguish four cases explicitly; do NOT conflate them, and do NOT
assume "one attempt-id" proves one process invocation — corroborate
with fixture invocation markers (sidecar files the fixture writes)
and process evidence (`/proc`, `pgrep`) alongside Dagu run/attempt IDs.

## Case 1 — worker process crash (SIGKILL)
`kill -9 <worker-pid>` mid-run.
Watch: dagu run state transition + timing; whether the coordinator
detects heartbeat loss (graceful SIGTERM wrote `aborted` itself —
SIGKILL may behave differently); fixture child-process fate (orphan
vs killed); portal state + termination reason; late artifacts after
worker restart; attempt-id uniqueness; NO second invocation.

## Case 2 — connection closure/reset (NOT silent partition)
A forwarding proxy `socat TCP-LISTEN:50056,fork,reuseaddr
TCP:127.0.0.1:50055` sits between worker and coordinator; killing the
proxy delivers an orderly close/reset. Label this CONNECTION LOSS —
it does not simulate a silent partition.
Watch: worker reconnect behavior; dagu run outcome; portal state;
attempt IDs; late evidence.

## Case 3 — silent traffic interruption (true partition surrogate)
Requires the connection to STOP MAKING PROGRESS WITHOUT an orderly
close — no RST/FIN. Options (no sudo, no host firewall):
(a) a purpose-written TCP-shim that, on signal, drops into a
    read-discard/write-suspend loop while keeping the socket open;
(b) `unshare -rn` netns + `nft`/`tc` inside the namespace only —
    host rules untouched.
The shim is preferred: its behavior is explicit and the test names
exactly what it simulates (established connection, traffic silently
black-holed, both endpoints still believe the socket is open).
Watch: heartbeat timeout path; whether the run stalls vs aborts;
worker-side state; portal state + reason; late delivery after heal.

## Case 4 — reconnection + late evidence
Heal the link / restart the worker after cases 2-3.
Watch: new dispatches work; prior-run artifacts upload late; portal
collects and records provenance; no duplicate invocations.

## Guardrails
- Loopback binds only; dedicated dirs; recorded PIDs; targeted
  cleanup. No sudo, no host-firewall changes, no external connections.
- Invocation counting: fixture writes a unique marker file per
  invocation + the portal/Dagu attempt IDs — both corroborated.
