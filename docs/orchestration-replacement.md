# Orchestration replacement: Dagu → Buildbot

Status: **implemented — awaiting acceptance gates** on
`feature/buildbot-orchestration` (draft PR #3). Adapter, dispatch,
execution-target model, Admin UI, SSH path, provisioning, and both
worker installers are code-complete and live-verified on the dev
topology; the release gates below remain open.
PR: draft, stacked on `feature/cheroot-frontend` (PR #2). No Cheroot
dependency; the base is shared so the fixed Dagu dispatch semantics
(`bea4874`, `8f3c1cf`, `df8fd8d`, `bf8d4b8`, `1adf71b`) stay in scope
while the replacement is built.

TestOrchestrator remains **authoritative** for user schedules, run IDs,
portal status, reporting, and evidence. Buildbot only queues, executes,
and stops jobs. No user schedule is ever configured in Buildbot — the
portal's `fire_due_schedules` tick keeps firing executions into the
orchestrator the same way it does today.

## 1. Dagu responsibility inventory → replacement mapping

| # | Dagu responsibility today | Where it lives now | Replacement owner |
|---|---|---|---|
| 1 | Accept a submission and create an orchestrator-side run identity | `DaguAdapter.submit` → `dagRunId` | Buildbot master REST `POST /api/v2/forceschedulers/…` → buildrequest id, persisted as `dispatch_id` (`bb:<brid>`) |
| 2 | Queue work until capacity is free | Dagu coordinator queue + `singleton` | Portal capacity CAS **before** submission (unchanged); Buildbot buildrequest queues on the target builder |
| 3 | Manual + scheduled dispatch trigger | `fire_due_schedules`, `create_execution` outbox `dagu-dispatch` | Unchanged portal code — outbox kind renamed to `orchestrator-dispatch`; transport decides adapter |
| 4 | Worker/runtime selection + explicit picks | `_select_runner_runtime`, `_assign_runner` | **Portal** — unchanged. Explicit `requested_runner`/`requested_runtime` honored or visibly rejected; never silently retargeted |
| 5 | Route a run to exactly one remote worker | `worker_selector={"kre": label}` + `remote_worker_names` uniqueness check | One dynamic builder per execution target: `to-target-<target_id>` bound to `workernames=[<bb worker name>]` |
| 6 | Coordinator↔portal credential | `coordinator-agent` secret + `server.yaml` basic auth (preserved on rerun since `bea4874`) | Per-worker `workername`/`password` pairs stored in the portal secrets store; localhost-only REST token for the adapter. Both sides written by the same provisioning step (atomically updated) |
| 7 | Cancel a queued run | `dagu stop` CLI / `POST …/stop` | `PUT /api/v2/buildrequests/{brid}` `control=cancel` — verified pre-claim |
| 8 | Cancel a running run | same | `PUT /api/v2/builds/{buildid}` `control=stop` — buildbot kills the remote command's process tree on the worker |
| 9 | Reconcile run state → portal state | `reconcile_dagu_runs` (+ `dagRunDetails`/`dagRun` envelope unwrap) | `reconcile_orchestrator_runs` — polls `buildrequests`/`builds`, maps `results` to portal states. Same semantics: aborted-with-request → `CANCELED`, unrequested abort → honest uncertainty, finished-no-result → `UNKNOWN` |
| 10 | Step/run logs | `DaguAdapter.logs`/`run_log` | `GET /api/v2/steps/{id}/logs/{logid}/contents` on the localhost REST API |
| 11 | Reports + artifacts to portal | Coordinator-hosted artifact store, `list_artifacts`/`download_artifact` | Buildbot `DirectoryUpload`/`MultipleFileUpload` worker→master file transfer; artifacts land in a master-side dir the portal reads directly (no second auth hop) |
| 12 | Source delivery to remote host | `git.checkout` action in the ad-hoc DAG | Standard `steps.Git` on the worker. Same credential boundary: worker-side ssh key path or unauthenticated remote only — **never** a secret in a build property (properties are visible via the API) |
| 13 | KRE invocation | `exec` step with portal-built argv | `steps.ShellCommand` with the same `kre_adapter.build_args` argv — no shell, validated runner spec, no free-form command from the portal |
| 14 | Workspace cleanup on worker | `handler_on.exit` rm + `to-ws-cleanup` enqueue + marker sweep | Cleanup step in the builder + same portal-side marker sweep driving a `to-cleanup` builder |
| 15 | Workflow definition | `deploy/dags/testorchestrator-execution.yaml` + `agent --once --monitor` step | Generated `master.cfg` builder/step definitions — no agent, no monitor shim |
| 16 | Persistent local agent executing work | `testorchestrator.agent` inside the Dagu workflow | **Eliminated on orchestrated transports** — the local buildbot worker runs the steps directly. `agent.py`/`agent_api.py` stay only while the Dagu path exists |
| 17 | Remote worker distribution | `3rdparty/dagu` archives, `to-worker` launcher, NSIS `testorchestrator-dagu-worker-*-setup-UNSIGNED.exe` | Bundled `buildbot-worker` wheels; `TestOrchestrator-Buildbot-Worker-Setup.exe` (NSIS) downloadable from Admin UI; Linux reproducible install + systemd unit |
| 18 | Worker label/identity | `--worker.labels kre=<tag>` + `--worker.id` | Unique `workername` per execution target + per-worker password; installer writes them, portal shows `Awaiting registration` until PB connect |
| 19 | Coordinator provisioning | `scripts/provision_dagu.py` (idempotent, credential-preserving) | `scripts/provision_buildbot.py` — same contract: idempotent, preserves worker passwords + REST token on rerun, stages units + `master.cfg` |
| 20 | Coordinator health check | `DaguAdapter.health` (reachable + version + workflow installed) | `BuildbotAdapter.health` (reachable + version + expected builders present) surfaced on the targets page |
| 21 | `UNKNOWN` on ambiguity | lease expiry, `reconcile_stale`, never re-dispatch | Unchanged portal semantics — ambiguous submission/termination → `UNKNOWN`, capacity held, never blindly resubmitted |
| 22 | Queued-run redrive | `_redrive_queued_remote` for `dagu`/`dagu-worker` | Same function extended to `buildbot-*` transports; brid-pinned resubmission is idempotent |
| 23 | Remote abort ≠ user cancel | `_collect_remote` unrequested-abort → `FAILED`/infra | Same rule for buildbot `CANCELLED` results without a portal cancel request |

## 2. Target topology

```
portal (Flask/Waitress or Caddy/Cheroot front)
  └─ flask worker tick: outbox, schedules, capacity CAS, reconcile
  └─ BuildbotAdapter ──localhost──▶ Buildbot master (REST :bbapi, bound 127.0.0.1)
                                     │ PB :bbpb (per-worker auth)
        ┌────────────────────────────┼───────────────────────────┐
        │                            │                           │
  to-local worker             to-ssh-dispatcher            remote buildbot-workers
  (controller host)           worker (controller host)       (Windows .exe / Linux systemd)
        │                            │                           │
   KRE local runs         SSH exec + SFTP collect      KRE runs on target host
   (local runners)        (agentless SSH targets)      + Git checkout + uploads
```

- **Local KRE runs** (`buildbot-local` transport): builders on `to-local`.
- **Remote workers** (`buildbot-worker` transport): one builder per
  registered execution target; Buildbot routes by `workernames`.
- **Agentless SSH** (`buildbot-ssh` transport): builders on the dedicated
  `to-ssh-dispatcher` worker, which runs `to_ssh_exec` (paramiko): connect,
  host-key verify, run the validated runner spec, SFTP artifacts back.
  Nothing TO-side executes on the remote host — no worker, no agent, no
  callback port.

## 3. Adapter contract

`testorchestrator.adapters.orchestrator` defines the seam dispatch.py
consumes; `BuildbotAdapter` implements it. `DaguAdapter` remains until the
post-acceptance removal commit.

```text
submit(execution) -> dispatch_id        # brid, execution-correlated
cancel(dispatch_id) -> acknowledged     # queued + running
status(dispatch_id) -> normalized       # queued|running|success|failed|aborted|unknown
log(dispatch_id) -> text                # kre stdout/stderr step log
artifacts(dispatch_id) -> files         # master-side uploaded dir
workers() -> [{name, connected}]        # registration status for the UI
health() -> {reachable, version, builders_present}
```

Correlation rules carried over and re-tested:

- `to_execution_id` is set as a build property **and** the brid is
  persisted before side effects continue.
- A lost submission response is reconciled by querying buildrequests by
  property before any retry — **never blind resubmit**.
- Runner/runtime assignment commits **before** `submit` (the fixed
  `8f3c1cf` ordering) — a build can be claimed immediately.
- A cancel landing between assign and submit gets a `cancel` call right
  after submission (same guard as `_dagu_assign_and_submit`).
- `requested_runner`/`requested_runtime` are honored or visibly rejected —
  never retargeted.
- `CANCELED` only after acknowledged termination; unrequested abort stays
  uncertain; unreachable host during SSH cancel shows "cancellation could
  not be confirmed".

## 4. Configuration without controller edits

`master.cfg` is generated once at install. It reads the portal database
for enabled execution targets → emits per-target builders, per-worker
credentials, and the localhost-bound REST config. Admin UI target changes
(commit to DB) then trigger `buildbot reconfig` (SIGHUP) — settings take
effect with **no manual controller configuration and no restart**. Queued
buildrequests survive a reconfig; running builds finish under their
claim-time config.

Target disable/remove semantics (portal-side, never silent redirect):

- **Disable**: target leaves selection immediately; QUEUED executions for
  it stay queued (redrive is target-scoped) or fail visibly — never
  retargeted. Running builds are left alone unless explicitly canceled.
- **Remove**: rejected while non-terminal executions reference it.
  Registered worker credentials are revoked in the same reconfig.

## 5. Security

- Buildbot REST API bound to `127.0.0.1`; never proxied by Caddy/Cheroot.
  Buildbot's own web UI is not part of the product surface.
- Per-worker `workername`/`password`; PB port is the only inbound port a
  remote worker needs — workers dial out.
- No secrets in build properties, step argv beyond the existing
  credential-file indirection, logs, or artifacts. SSH private keys live
  in the portal secrets store and are written 0600 for the dispatcher
  only.
- SSH target: restricted account, unique remote run dir per execution,
  host-key fingerprint shown and explicitly approved in the UI, mismatch
  → reject + require re-verification, validated runtime spec only.
- Reconnect semantics: remote run dir persists `status`/`exit_code`/logs;
  a reconnecting dispatcher reattaches to the same execution instead of
  launching again.

## 6. Packaging and licensing — gates

- **GPL gate (blocking bundling):** `buildbot` and `buildbot-worker` are
  GPL-2.0. Development proceeds from PyPI in the dev venv; **no Buildbot
  code is bundled into a release artifact until redistribution approval
  is explicitly confirmed.** Recorded as an open gate in the PR.
- `paramiko` (SSH transport) is LGPL-2.1 — notice + source-link handling
  in THIRD-PARTY-NOTICES.
- Pins (dev): `buildbot==4.3.0`, `buildbot-worker==4.3.0` (2025-05-12 —
  vetted age); paramiko pinned once selected. All transitive deps pinned
  and wheels vendored before packaging.
- Size delta measured: compressed wheels + installed `site-packages`,
  reported against the Dagu baseline before any ship decision.

## 7. Transition plan

1. Contract (this document) + prototype force/cancel/reconcile on the pin.
2. `BuildbotAdapter` + `buildbot-*` transports beside the Dagu ones.
3. Admin → Execution Targets: three target types, UI-managed.
4. Worker installers (Windows NSIS `.exe`, Linux systemd), SSH dispatcher.
5. Packaged acceptance on all three target types.
6. Dagu removal commit: delete binary/archives/dag/provisioning/`DaguAdapter`/
   dagu transports/`dagu_*` settings; verify a Dagu-free build passes smoke.

`dagu_mandatory` → `orchestrator` backend setting; during development the
transport is chosen per execution target so Dagu keeps working until the
removal commit.

## 8. Acceptance and gates

| Gate | State |
|---|---|
| GPL redistribution approval | **open — required before bundling** |
| Real Windows machine for .exe acceptance | **open — needed** |
| Licensed KRE per target type | **open — no licence available** |
| Prototype: submit, queued-cancel, running-cancel, lost-response reconcile | **done** — live on 4.3.0 (§9) |
| Local target E2E: portal → buildbot → worker → collect → normalize | **done** — live, `COMPLETED`/`passed` |
| SSH target E2E: portal → dispatcher → remote run → SFTP collect | **done** — live, `COMPLETED`/`passed` |
| Installer: `--orchestrator` provisioning, units, credential preservation | **done** — dev-host verified |
| Windows worker .exe: download → offline install → register → run | code-complete — **blocked** on real Windows machine |
| Linux worker remote install + registration | code-complete — remote-host verification pending |
| All three targets: dispatch, explicit pick, cancel, artifacts, disconnect/reconnect, queue saturation, controller restart | partially covered (tests + local/ssh E2E) — packaged sweep pending |
| Caddy + Cheroot spot-check of orchestration | pending |
| Dagu-free clean build + smoke | pending — after acceptance only |

Cypress / Playwright / Robot Framework: N/A — not shipped.

## 9. Prototype verification (Buildbot 4.3.0, scratch master + worker)

Verified live against `buildbot==4.3.0` + `buildbot-worker==4.3.0` +
`buildbot-www==4.3.0` (the REST API is a separate distribution —
`buildbot` alone does not provide it):

| Contract point | Verified behavior |
|---|---|
| Submit | `POST /api/v2/forceschedulers/{name}` JSON-RPC `force` → `{"<builderid>": <brid>}`. `builderNames:["to-target-x"]` or `builderid` restricts to exactly one builder — per-run targeting works without per-target schedulers |
| Correlation | `to_execution_id` declared as a `StringParameter` propagates to buildrequest AND build properties; `GET /buildrequests?property=to_execution_id` (value matched client-side) reconciles a lost response; `GET /buildrequests/{brid}/builds` maps brid→build |
| Queued cancel | `POST /api/v2/buildrequests/{brid}` `cancel` → `complete=true`, `results=6` (CANCELLED) |
| Running cancel | Same `cancel` call on a **claimed** brid stops the running build — results 6, remote `sleep` process confirmed killed on the worker. One cancel call covers both cases |
| Logs | `GET /builds/{id}/steps` → `GET /steps/{id}/logs` → `GET /logs/{id}/contents` (line-prefixed `h`/`o` stream) |
| Artifacts | `steps.DirectoryUpload(workersrc=…, masterdest=…)` lands worker files master-side under a portal-known dir — no second auth hop |
| KRE argv | `@util.renderer` on `ShellCommand.command` renders a JSON property to an argv list master-side — no shell, no injection |
| Dynamic config | `buildbot reconfig` applies a regenerated `master.cfg` in ~0.02 s — the UI→DB→reconfig model works; queued buildrequests survive it |
| REST auth | `UserPasswordAuth` + basic-auth `GET /auth/login` → `TWISTED_SESSION` cookie; adapter re-authenticates on expiry. Bound to `127.0.0.1` |
| Worker registry | `GET /api/v2/workers` shows `configured_on` vs `connected_to` — the UI's "Awaiting registration" vs "online" distinction |
| **Telemetry** | `buildbotNetUsageData` phones home **by default** — packaged `master.cfg` MUST set `c['buildbotNetUsageData'] = None` |
