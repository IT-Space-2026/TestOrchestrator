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

## Unattended options (documented contract)
```
worker-setup.exe /S /D=C:\TO\worker /ORCH_URL=https://orch.example /AGENT_TOKEN=...
uninstall.exe /S [/PURGEDATA=1]
```
`/S` = silent, `/D` = install dir, `/ORCH_URL` = central Orchestrator,
`/AGENT_TOKEN` = one-time registration token.

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
