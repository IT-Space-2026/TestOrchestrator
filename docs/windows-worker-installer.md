# Windows worker installer — design + honest status

**Status: scaffold, not yet executed on Windows.** The `.nsi` source,
payload assembly, and unattended contract are implemented; compiling
requires `makensis` (runs on Linux or a Windows build agent) and a
staged Windows embeddable Python runtime. No Windows host evidence
exists yet — this is a documented gate, not a claim.

## Role scope
**Worker/execution role only** — first target **Windows Server 2025
Standard (Desktop Experience)**. The installer does NOT contain the
central Orchestrator stack (Waitress/Caddy/Dagu/DB). A Windows
central-server role is out of scope until separately scoped and
validated. Windows 11 acceptance is a separate gate.

## What it must contain
- Windows embeddable CPython (x64) — a genuinely Windows runtime,
  never a repacked Linux binary.
- `testorchestrator` agent/worker sources + `agent_service.py` +
  `register_agent.py` (registration handshake to the central
  Orchestrator: URL + one-time token exchanged for a stored
  credential — the token is not left in config).
- Execution mechanism: Scheduled Task `TestOrchestratorWorker`
  (ONSTART, SYSTEM, highest) — a native mechanism, no third-party
  service wrapper dependency. (A Windows Service via `sc.exe` +
  `srvany`-equivalent was rejected: it needs non-native shims.)
- Data dirs under `%PROGRAMDATA%\TestOrchestrator` — preserved across
  uninstall by default; `/PURGEDATA=1` + explicit confirm to delete.

## Credential & account model (security-reviewed)

- **No reusable token on the command line.** `/AGENT_TOKEN=` is removed:
  command lines land in process lists, install logs, and (worse) inside
  the generated Scheduled Task definition. Enrollment instead uses a
  **short-lived, single-use enrollment credential**:
  - unattended: `/ENROLL_FILE=<path>` — read once by
    `register_agent.py`, exchanged for a worker credential, then
    **deleted**;
  - interactive: a password-style input page — never echoed, never
    written to `install.log`.
- **Stored credential:** the exchanged worker credential lives as a
  DPAPI-protected file under `%PROGRAMDATA%\TestOrchestrator`, ACL'd to
  the task account only. Rotation = re-run `register_agent.py --rotate`
  (invalidates the old credential server-side); rotation is also forced
  when a new enrollment credential is issued for the worker.
- **Task account:** SYSTEM is NOT assumed required. Design target is a
  **dedicated least-privilege account** (e.g. `TO-WORKER` service
  account with log-on-as-batch). SYSTEM is kept in the script only
  until KRE licence activation, repository access, and execution are
  proven under the dedicated account — that evidence is an open gate.
- **Headless vs interactive desktop:** the worker runs suites headless
  by design. Any suite requiring an interactive desktop session is
  explicitly out of the task-account model and must be run under an
  interactive logon configuration — a documented separate mode, not a
  silent fallback.

## Unattended options (documented contract)
```
worker-setup.exe /S /D=C:\TO\worker /ORCH_URL=https://orch.example /ENROLL_FILE=C:\secure\enroll.cred
uninstall.exe /S [/PURGEDATA=1]
```
`/S` = silent, `/D` = install dir, `/ORCH_URL` = central Orchestrator,
`/ENROLL_FILE` = short-lived enrollment credential (deleted after use).

## Java on the worker
A worker does **not** require Java when report generation (Allure)
happens centrally. The Windows installer performs no Java checks —
that dependency belongs to whichever host generates reports.

## Signing
Unsigned builds are labelled `-UNSIGNED.exe`. Signed builds wait for
an approved signing identity — never ask users to disable Windows
security controls (SmartScreen etc.).

## Remaining gates
makensis compile, payload staging, clean-Server-2025 install, register
+ approved-KRE-suite execution, cancellation/reconnect, reboot health,
uninstall-preserves-data — all open until demonstrated on the host.
