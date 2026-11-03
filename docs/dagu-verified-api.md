# Dagu 2.25.2 API — verified against the bundled binary (2026-11-03)

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
  (`{"status":"healthy","version":"2.25.2",...}`); `/api/v1/version`
  does NOT exist on 2.25.2 (404).

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
