# Dagu API — verified against the bundled binary (pin now 2.27.0)

> **SUPERSEDED (2.5.0):** Dagu was replaced by Buildbot 4.3.0 as the sole orchestrator (see `docs/orchestration-replacement.md`). This document is retained for history only and does not describe the shipped architecture.


Originally verified 2026-11-03 vs bundled 2.25.2; re-verified
2026-11-06 vs bundled 2.25.8 — `/api/v1/health` reports
`{"status":"healthy","version":"2.25.8"}`, `/api/v1/version` still 404,

**Pin updated to 2.27.0 (2026-11-16):** contract suite re-run against
the bundled 2.27.0 binary — provision + basic-auth roundtrip
(`/health` reports `version: 2.27.0`, unauth/wrong-cred → 401) and
submit → status → cancel all pass. Endpoint behaviour below is
unchanged; remote-worker path re-verified live in the 2.0 E2E rerun.
and the contract tests (submit → dagRunId → status → CLI stop →
aborted, plus provision→basic-auth roundtrip) pass against the new
archive unchanged.

Server: `dagu start-all --port 29999 --config server.yaml --dags <dir>`
(auth.mode: none used for dev probing only).

## Authentication (verified 2026-11-03, runbook path)

- `auth.mode: basic` + `auth.basic.username/password` (or
  `DAGU_AUTH_BASIC_*` env): API accepts `Authorization: Basic`, rejects
  no/wrong credentials with 401. **This is the provisioned mode** —
  `scripts/provision_dagu.py` and the adapter's `dagu_username` setting.
- `auth.mode: builtin` uses `token.secret` as the **JWT signing secret** —
  a static Bearer against it returns 401; builtin requires an issued API
  key/JWT (adapter supports Bearer for that case).
- Health/version endpoint is `GET /api/v1/health`
  (`{"status":"healthy","version":"2.25.8",...}`); `/api/v1/version`
  does NOT exist on 2.25.8 (404 — re-verified 2026-11-06).

## Verified live
- `POST /api/v1/dags/{name}/start` → `{"dagRunId":"034r5YZLmYSiJgWtO63bLI"}`
  — confirmed run identity returned.
- `GET /api/v1/dags/{name}/dag-runs/{id}` → run object with `statusLabel`,
  `finishedAt`, `log` path, `nodes[]`. Status values: `running`,
  `succeeded`, `aborted`.
- `GET /api/v1/dags/{name}/dag-runs/latest` → latest run object.
- DAG step execution proven: fixture `to-fixture.yaml` ran
  `echo EXECUTOR_INVOKED` — server-side run completed `succeeded`,
  evidence in `logs/<dag>/dag-run_*/dag-run_*.log`.
- Cancellation: `dagu stop <name> --run-id <id>` → run went `aborted`
  (CLI path proven; HTTP cancel endpoint path NOT yet located —
  404/405 on the probed variants).
- API enforces Bearer auth by default (`401` + `Www-Authenticate: Bearer`);
  `auth.mode: none` disables it (dev only).

## Not yet verified
- HTTP cancel endpoint path (probed `stop|cancel|abort` on several
  routes → 404/405); CLI stop works. Logs via API (only log file path
  observed). Queueing/serialisation semantics. Webhook back-channel.

## Orchestration ownership implemented
`dispatch._handle_event` (dagu-dispatch): submit → persist `dispatch_id`
→ set DISPATCHED → return. The portal does NOT call `run_execution`
after submission when mandatory; the DAG step
(`deploy/dags/testorchestrator-execution.yaml`) invokes the authenticated
agent executor (`--once`, bearer token, immutable snapshot) instead.
