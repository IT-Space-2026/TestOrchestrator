# Integration contracts

Version pins, endpoint shapes, and verification status per integration.
"Unverified" means the client exists but has never been exercised
against a live service.

## Buildbot (orchestration) — `testorchestrator/adapters/buildbot.py`

- Role: execution orchestration only; the portal owns immutable
  execution definitions, scheduling, and authorization.
- Master: **4.3.0 bundled**, loopback-only REST
  (`http://127.0.0.1:8010/api/v2/`); PB listener on `:9989` for
  workers. Provisioned at install by `scripts/provision_buildbot.py`
  (idempotent, credential-preserving on rerun).
- Auth: REST uses HTTP Basic (`buildbot_username` +
  `buildbot_credential_ref` SecretRef); workers authenticate over PB
  with per-worker passwords issued/rotatable via the Admin UI.
- Operations: `POST` buildrequest (submit), `GET` build/buildrequest
  (poll/reconcile), `POST` control cancel; assignment commits BEFORE
  the buildrequest exists, so a claimed build can never outrun the
  DISPATCHED row. Ambiguous timeout → reconcile, never blind
  re-submit. Dispatch state is durable (`outbox_events`), survives
  restart.
- Workers: `to-local` (controller-local), `to-ssh-dispatcher`
  (agentless SSH), remote `buildbot-worker` 4.3.0 hosts — all three
  paths exercised end-to-end on a clean install.
- Status: **verified live** on the 2.5.0 candidate
  (`docs/acceptance-2.5.0.md`).

## Agentless SSH — `scripts/to_ssh_exec.py`

- The `to-ssh-dispatcher` worker launches a detached remote process
  group (`setsid` session leader writes its PGID to `run.pid`),
  monitors over the exec channel, and collects results over SFTP.
  Channel fds are fully redirected so the exec channel closes
  immediately — no remote shell holds it open for the run lifetime.
- Host keys are pinned through the Admin UI (first-contact capture,
  explicit approve, verified fingerprint before any auth).
- Cancel: portal-side `_ssh_remote_cancel` verifies the approved
  fingerprint, reads `run.pid`, and terminates the remote process
  group (`TERM` then `KILL`, confirmed via `kill -0`) — a CANCELED
  state is only recorded after confirmed termination, otherwise the
  kill is logged as `UNCONFIRMED`.

## KRE (execution) — `testorchestrator/adapters/kre.py`

- Boundary: connect-only. Never installs/imports/upgrades/licenses KRE.
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
- Invocation credential (residual exposure — documented): a runtime
  `credential_ref` resolves to the activation value, passed to the child
  as `-apiKey=<value>` — the vendor-documented KRE mechanism; no
  vendor-supported env/file alternative is confirmed for console
  activation. **Residual risk:** the key is visible in the child's argv
  to same-UID users on the runner host (`/proc/<pid>/cmdline`, `ps`)
  for the process lifetime. Mitigations in place: value is redacted
  from `kre-invocation.log`, stdout/stderr are scrubbed post-run, no
  secret lands in artifacts/audit/Git export/UI; restricted host
  permissions (0600 credential files) remain required. If a
  vendor-supported non-argv mechanism is confirmed for the operator's
  KRE version, adopt it — `run_invocation` already supports
  `env_extra` injection.
- Status: mechanics proven vs. fixture executable
  (`tests/test_pipeline.py`); **real KRE CLI/flags/exit codes
  unverified** — pending a licensed operator binary.

## Git — `testorchestrator/gitsync.py` + `git_backend.py` (Dulwich default)

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
