# Windows worker acceptance kit — 2.6.0 release blocker

Repeatable procedure for verifying Robot Framework and pytest on a
real Windows execution host. **Nothing in this file has been run** —
the 2.6.0 candidate has never executed on Windows. Completion of
every mandatory row is required before the release candidate ships;
each row must record execution ids, build numbers, and the exact
tested commit + package SHA-256.

Do not relabel synced-working-tree evidence as Windows evidence —
Windows claims must come from this procedure on the packaged build.

## 1. Prerequisites

| Item | Requirement |
|---|---|
| Host | Windows 10/11 amd64 or Windows Server 2019+, reachable over outbound TCP to the controller |
| Controller | Clean packaged 2.6.0 install (Linux or Windows) at a pinned commit — record `toctl install-info` + package SHA-256 |
| Worker installer | `TestOrchestrator-Buildbot-Worker-Setup-<ver>.exe` downloaded from **System → Infrastructure** of the same candidate, SHA-256 verified |
| Python | For pytest/robot runtimes: install Python 3.11+ on the worker host and register its path as the runtime executable |
| Frameworks | `pip install pytest pytest-xdist robotframework robotframework-pabot` on the worker host (versions recorded) |
| PB port | Controller accepts inbound TCP 9989 from the worker host |

## 2. Install + register

1. Admin → Infrastructure → **Add target** → type *Windows Buildbot
   worker*. Record the generated worker name + one-time password
   (shown once, stored encrypted).
2. Copy the installer to the Windows host; verify SHA-256; run
   (SmartScreen warning expected — unsigned build).
3. Provide worker name, controller PB endpoint
   `<controller>:9989`, and password when prompted (or
   `/PASSWD_FILE` for unattended).
4. Target row flips from `Awaiting registration` to connected; run
   **Probe** — expected `ok` with OS/arch reported as Windows.
5. Register runtimes under the worker: `pytest` and `robot` Runtime
   connections pointing at the Windows Python (e.g.
   `C:\Python312\python.exe`).

## 3. Acceptance matrix (all mandatory)

| # | Scenario | Steps | Expected |
|---|---|---|---|
| W1 | pytest sequential | Run a discovered pytest suite on the Windows runner | COMPLETED; junit normalized; per-case outcomes correct |
| W2 | Robot sequential | Run a `.robot` suite via the `robot` runtime | COMPLETED; output.xml merged into results; log.html/report.html uploaded |
| W3 | pytest parallel (xdist) | `parallel: 2` run config | Both units launch (`-n 2`); parent aggregates; reservations show procs=2 |
| W4 | Robot parallel (Pabot) | `parallel: 2` | Two `robot -A` children; merged output; pabot_results retained |
| W5 | Cancellation | Cancel a long pytest run mid-execution | CANCELED; remote process tree confirmed dead; slot released |
| W6 | Worker PB loss + retry | Kill `buildbot-worker` service mid-run; restart | prep counts attempt 2; pidfile/env-marker resolution runs (POSIX-gated — on Windows the raw-argv contract applies, no pidfile wrapper); retry bounded by `max_attempts`; attempts + audit recorded |
| W7 | Cancel racing submission | Cancel immediately after Run click | Never-launched cancel → CANCELED without build; racing cancel → honest state, no orphaned reservation |
| W8 | Queue capacity | Occupy both slots; queue a third run | `wait_reason` shown ("job slots full 2/2"); FIFO dispatch on release; reservations open then released |
| W9 | UNKNOWN + reconciliation | Force ambiguity (worker powered off mid-run, no retry path) | UNKNOWN; reservations held; appears on `/reconciliation`; admin resolve with note audits + frees claims |
| W10 | Schedule | Interval schedule on the Windows target | Fires once per interval; overlap/misfire policy honored; audit rows present |
| W11 | Manifest + downloads | Any W1–W4 run | `manifest.json` sealed in report dir; API verify → all `ok`; every artifact downloads 200 |
| W12 | Webhook | Terminal run with endpoint configured | `execution.completed/failed` delivered, HMAC-signed, `X-TO-Event` header present |

## 4. Evidence to capture per row

- Execution id + Buildbot buildrequest/build numbers + attempt list
  (`snapshot.bb_attempts`).
- Step stdio for `prep`/`run` on retry scenarios.
- Windows-side: worker service log, builddir contents, process tree
  (`Get-Process` / Process Explorer screenshot for orphan checks).
- `deployed_rev` from `/api/v1/ping` and the execution snapshot.
- Package + installer SHA-256s; git commit of the controller.

## 5. Known platform differences (expected, not defects)

- **No pidfile wrapper on Windows**: `posix=False` execs run the raw
  argv; `.to_exec_pid`/`.to_exec_attempt` are POSIX artifacts.
  W6/W7 on Windows rely on Buildbot's worker-loss retry + the
  reconciler's attempt tracking instead — confirm the retry still
  records distinct attempts and never double-executes.
- Path separators / case-insensitive FS in artifact staging.
- `sh`-based steps are POSIX-only by design; the prep step is
  `doStepIf=_posix`-gated and skipped on Windows.

## 6. Sign-off

All mandatory rows PASS or are recorded with an explicit
release-blocking defect + commit reference. Record the completed
matrix into `docs/acceptance-2.6.0.md` with commit + package SHA-256
before the packaged smoke matrix proceeds.
