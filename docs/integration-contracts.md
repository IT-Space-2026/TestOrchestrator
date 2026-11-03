# Integration contracts

Required by handoff §3. Version pins, endpoint shapes, and verification
status per integration. "Unverified" means the client exists but has never
been exercised against a live service — see `implementation-status.md`.

## Dagu (orchestration) — `testorchestrator/adapters/dagu.py`

- Role: workflow orchestration only; the portal owns immutable execution
  definitions and authorization (§7).
- Client: HTTP REST under `{dagu_endpoint}/api/v1/`. Auth: HTTP Basic
  (`dagu_username` + `dagu_credential_ref` SecretRef holding the password)
  **verified against bundled 2.25.2** (`auth.mode: basic`); alternatively a
  builtin-mode Bearer API key via the same SecretRef. Timeout 30s.
- Operations: `GET /health` (status+version), `GET /dags` (workflow
  presence), `POST /dags/testorchestrator-execution/start`,
  `GET .../dag-runs/{id}`, `GET .../log`; cancel via verified CLI
  `dagu stop <dag> --run-id <id>` (HTTP cancel path unresolved on 2.25.2).
- Contract: submission returns external run identity persisted in
  `executions.dispatch_id` BEFORE any side effect; ambiguous timeout →
  reconcile, never blind re-submit. Dispatch state is durable
  (`outbox_events`), survives restart.
- Pin: **2.25.2 bundled**; submit/status/cancel + basic-auth health
  verified live (`docs/dagu-verified-api.md`, contract test).

## KRE (execution) — `testorchestrator/adapters/kre.py`

- Boundary: connect-only. Never installs/imports/upgrades/licenses KRE (§5).
  Executable presence ≠ licence readiness.
- Invocation: subprocess argv array (no shell), `start_new_session=True`
  (process-group kill for tree cancellation), per-attempt isolated
  workspace at recorded source SHA + dedicated report dir.
- Args (Katalon-documented shape, version-aware, may need per-version
  overrides): `-noSplash -runMode=console -projectPath= -testSuitePath=
  -executionProfile= -browserType= -reportFolder=`.
- Probe: `<exe> -version` (30s). Captures stdout/stderr to `logs/`,
  timestamps, exit status, PID. Error classes: license/config/
  infrastructure/execution — missing results never pass as a run.
- Status: mechanics proven vs. fixture executable (`tests/test_pipeline.py`);
  **real KRE CLI/flags/exit codes unverified** — pending operator binary.

## Local runner — `testorchestrator/dispatch.py`, `adapters/runner.py`

- Registration fields per §6 (name/desc/os_arch/mode/projects/tags/
  workspace/slots/health/heartbeat/drain/credential_ref).
- Scheduling: enabled ∧ not draining ∧ allowed project ∧ free slots.
  Leases (`lease_expires_at`, 60s) + heartbeats; expired lease → UNKNOWN,
  never duplicate dispatch. Drain stops new work, lets active finish.
- Remote transport: **not implemented** — decision recorded: secured Python
  agent (§6 option A). Remote-mode runners are never assigned.

## ReportPortal — `testorchestrator/adapters/reportportal.py`

- Strategy: direct API publication (§9 option 1); JUnit-import path NOT
  used (cannot map screenshots to attempts).
- Client: `{rp_endpoint}/api/v1/{project}/launch|item|log/entry|finish`,
  Bearer token via `reportportal_credential_ref`. Attachments carry MIME +
  checksum-matched content; per-artifact `rp_state` + `rp_remote_id`.
- Ambiguity: remote timeout may mean accepted — retries reconcile on
  persisted IDs, never recreate hierarchy blindly.
- Status: **implemented, unverified** — needs live RP instance. A02
  (screenshot→case mapping in RP) unproven.

## Git — `testorchestrator/gitsync.py` (GitPython)

- Source bindings: clone/fetch into `instance/bindings/<id>`; read-only
  index (case/suite listing); no code execution, no auto submodules.
- Destination export: `git_export_repo` setting; layout under
  `TestOrchestrator/projects/<pid>/{suites,plans,releases,executions/<id>}`
  with snapshot/summary/results/reporting.json + `evidence/<kind>/`.
- Export whitelist: junit/log/screenshot/html only — no secrets, users,
  sessions, keys, licences, live DB, HAR/video/debug captures. Checksum
  re-verified on copy; clean-tree commits are no-ops.
- Auth: `git_export_credential_ref`/binding `credential_ref` resolve via
  SecretRef; HTTPS/SSH remote transports unverified (local paths tested).

## Secrets — `testorchestrator/secrets_store.py`

- Fernet (cryptography lib) encryption; key file `TO_SECRET_KEY_FILE`
  (default `instance/secret.key`, 0600) outside the DB. Names/kinds/meta in
  DB only — values never in settings/snapshots/audit/Git.
