# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2.6.0 (draft)

Feature release — Playwright Test and headless Cypress E2E as
first-class runners on the existing Buildbot/SSH execution paths.
Schema change: `runtime_connections.runtime_kind`
(`kre`|`playwright`|`cypress`, default `kre` — existing connections
upgrade unchanged).

### Added

- **Node.js browser runners.** Register a runtime with kind
  `playwright` or `cypress` on any Buildbot worker (Windows/Linux)
  or agentless Linux SSH target. Runs stage the project under
  `build/src` and invoke `npx playwright test --reporter=junit` /
  `npx cypress run` (reporter `junit`, `toConsoleStandard:true`,
  per-spec `junit-[hash].xml` names so later specs cannot overwrite
  earlier reports). JUnit, logs, traces, screenshots and videos land
  in the standard Reports/ upload → existing `parse_junit`
  normalization and artifact retention — same run IDs, same
  pipeline.
- **Check runner readiness** action on the Infrastructure page —
  runs a probe build on the *same target, account and transport*
  the real run uses (remote worker via its own builder; agentless
  SSH via the dispatcher over SSH/SFTP). Reports connectivity,
  Node.js/framework/browser versions, and browser reachability of a
  configured application URL. Installs nothing, runs no tests,
  echoes no secrets; results persist on the runtime record.
- **Browser selection** for node runtimes — validated against the
  framework's supported set (`chromium`/`firefox`/`webkit`;
  `chrome`/`firefox`/`edge`/`electron`/`chromium`), invalid values
  rejected, never silently substituted.
- **SSH run spec** gains `exec_subdir` and `env` — the generated
  remote `run.sh` cd's to the staged project root and exports
  framework env (e.g. `PLAYWRIGHT_JUNIT_OUTPUT_FILE`) before exec.
- **UI guidance**: runtime-kind selector, per-target connection-
  requirements table, and prerequisites text on the run forms and
  Infrastructure page.

### Notes

- Node.js, the project's Playwright/Cypress dependencies, and the
  browsers are **pre-provisioned on the target** — nothing is
  bundled into the controller packages or the worker installer.
- Unsupported browsers are rejected up front; a missing JUnit report
  can never surface as a green run with zero tests (existing
  `REPORTS-MISSING` sentinel unchanged).
- In scope for 2.6.0: Playwright Test (Node.js) and headless
  Cypress E2E only — Playwright-Python, Cypress component testing,
  remote-browser grids and Cypress Cloud are out of scope.

## [2.5.1] — 2026-11-19

Patch release — install fixes only; no schema or API changes.
Upgrades preserve all state (payload replaced, data/env/credentials
untouched, same reruns-are-safe contract).

### Fixed

- **Windows worker installer** failed on hosts without an existing
  pip: `get-pip.py --no-index --find-links wheels` reported *"No
  matching distribution found for pip"*. A pinned `pip` wheel is now
  bundled into the worker payload's `wheels/` — same fix applied to
  the Linux worker bundle's `bb-wheels/` for parity.
- **Windows controller installer** now offers a default-checked
  *"Open TestOrchestrator in your browser"* option on the finish
  page, opening `https://<addr>:<port>/` in the default browser
  (skipped automatically for silent `/S` installs).
- **Windows worker did not start after install**: the Scheduled
  Task command line was mangled twice — wrong argument order
  (`--nodaemon start`) and embedded `/TR` quotes broken by the
  NSIS→`cmd /c`→`schtasks` chain. Task registration now happens
  inside `setup-worker.py` via `subprocess` argument lists (no
  shell quoting) as `buildbot-worker.exe start --nodaemon`, and the
  task is started immediately after registration.
- **Windows worker tray icon**: a Startup-folder shortcut launches
  `worker-tray.py` (pystray + Pillow bundled offline) at user logon —
  notification-area icon showing worker state with right-click
  **Start / Stop / Quit** (controls the Scheduled Task via schtasks;
  Quit only closes the icon, never the worker). The menu also has
  **Configure…** — re-prompts endpoint/workername/password (blank
  keeps current), recreates `buildbot.tac`, restarts the task; a
  Start Menu shortcut + `start-tray.cmd` relaunch the icon after
  Quit. Uninstall kills only
  the tray process (targeted `stop-tray.cmd` — never a blanket
  `taskkill` on `pythonw.exe`).
- **Windows controller firewall**: SYSTEM Scheduled Tasks never
  trigger the interactive "allow access" prompt, so inbound
  connections were silently dropped — remote workers could never
  reach PB `:9989` and stayed at `Awaiting registration`. The
  installer now adds inbound rules for the portal ports and
  `:9989`, removed on uninstall.
- **Controller desktop shortcut** added on install — opens the
  portal login page (`https://<addr>:<port>/`) in the default
  browser; removed on uninstall.
- **Infrastructure UI**: worker-row actions no longer overflow the
  table — secondary actions (Issue password, Drain/Resume,
  Enable/Disable, Remove) moved under a compact **Manage ▾** menu;
  the Workers card now documents the Buildbot PB endpoint
  (`<host>:9989`, not the https URL) beside the download table.

### Documentation

- All screenshots recaptured from the released codebase; step-by-step
  install walkthrough added to the Admin Manual (§4.2, one
  screenshot per step); architecture/dataflow diagrams re-rendered;
  Administrator and User Manuals also ship as PDF release assets.

## [2.5.0] — 2026-11-19

Initial public release.

### Portal and frontend

- Flask/Jinja2 portal served over HTTPS by **Cheroot, in-process** —
  no separate web-tier binary or unit. Waitress remains internal
  loopback only.
- **Admin → Network & certificates** manages TLS material, HTTPS
  listener, and the optional HTTP→HTTPS redirect listener.
  Certificate changes apply live; listener changes are staged
  through a last-good snapshot with automatic rollback.
- Session security: Argon2 password hashing, CSRF protection,
  `Secure`/`HttpOnly` cookies, forced bootstrap password change,
  role-based access (admin/operator/viewer).

### Orchestration — Buildbot 4.3.0

- **Buildbot master** on loopback REST (`:8010`) + PB (`:9989`),
  provisioned and managed by the installer and the portal —
  credentials are generated, stored encrypted, and preserved across
  reruns.
- Controller-side workers: `to-local` (local execution) and
  `to-ssh-dispatcher` (agentless SSH).
- Three execution modes, all managed from **Admin → Workers &
  runtimes**:
  1. **Local worker** — runs on the controller host.
  2. **Remote Buildbot worker** — bundled offline installer
     (Windows `.exe` / Linux script), outbound PB with per-worker
     credentials issued and rotatable through the Admin UI.
  3. **Agentless SSH** — `sshd` + the test runtime on the host;
     controller-side dispatcher launches and monitors over SSH/SFTP
     with Admin-UI host-key approval. No agent install.
- Dispatch integrity: assignment commits before the buildrequest
  exists, explicit runner/runtime picks are honored or visibly
  rejected, cancel propagates to remote process groups with
  confirmed or honestly-flagged termination, and recovery after
  disconnect resumes in-flight work without duplicates.
- Execution history is durable and never deleted by upgrades;
  cancellation intent is preserved across reconciliation.

### Evidence and reporting

- Per-execution evidence collection (logs, JUnit, artifacts) with
  configurable retention and `toctl evidence-prune`.
- Vendored **reportingLabs** report generation served with
  generation-scoped CSP sandboxing; idempotent regeneration.
- Optional git-based evidence export.
- Audit trail for administrative actions, independent of
  operational logs.

### Application licence

- The application licence is **informational only** — it never
  gates any feature. Without a valid licence the login page shows
  an informational notice and nothing else changes.

### Packaging

- Linux x86_64: tarball + self-extracting `.run`, `install.sh`
  (`toctl install`) with offline payload, install-time migrations,
  systemd unit staging, idempotent reruns that preserve data,
  credentials, and secrets.
- Windows amd64 controller: unsigned `.exe`
  (`build-scripts/build_windows.py` →
  `packaging/windows/app-installer.nsi`) — embedded Python,
  `buildbot-py` runtime (master + `to-local` + `to-ssh-dispatcher`),
  five Scheduled Tasks, persistent `%ProgramData%\TestOrchestrator`
  state, per-install CA trusted via `certutil`, data retained on
  uninstall unless `/PURGEDATA=1`.
- Windows amd64 Buildbot worker: unsigned offline `.exe` served
  from the installed portal next to its matching
  corresponding-source archive.
- Linux x86_64 Buildbot worker: `install_linux_worker.sh` + offline
  wheel closure (py3.10–3.12) + `get-pip.py` fallback in one
  downloadable bundle — the worker host needs no network.
- GPL third-party components ship as separate processes with
  licence texts, notices, and a written corresponding-source offer
  fulfilled by IT-Space Kft. (see `3rdparty/CORRESPONDING-SOURCES.txt`
  and `docs/redistribution-review.md`).
