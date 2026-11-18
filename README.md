# TestOrchestrator

<p align="center"><img src="assets/branding/iT-space-logo-400x400.png" alt="IT-Space Kft. logo" width="120"></p>

Test orchestration web application for **IT-Space Kft.** — Flask + Jinja2
server-rendered UI, an in-process Cheroot HTTPS frontend, Buildbot
execution orchestration, Katalon Runtime Engine (KRE) execution via
managed execution targets, and reporting-labs single-file reports —
a pure-Python backend with no bundled Node.js.

**Status: 2.5.0 released** ([CHANGELOG](CHANGELOG.md) ·
[release](https://github.com/IT-Space-ltd/TestOrchestrator/releases/tag/2.5)).
2.5.0 replaces Dagu with Buildbot as the sole execution orchestrator
(local workers, remote Buildbot workers, and agentless SSH targets —
all managed from Admin → Execution Targets) and makes the in-process
Cheroot tier the shipped frontend; neither Dagu nor Caddy ships. Prior released line: **v2.0.3** — the tag
resolves to commit `be5d1335bb82e9e49566a4d1f42fa710dd2cf6de`
([release notes](docs/release-notes-2.0.3.md) ·
[GitHub release](https://github.com/IT-Space-ltd/TestOrchestrator/releases/tag/v2.0.3)).

## Manuals

- **[User manual](https://github.com/IT-Space-ltd/TestOrchestrator/blob/main/Manuals-Doc/User-Manual.md)** — sign-in, roles,
  projects and repository bindings, worker runs, historical-result
  import, reports and evidence.
- **[Admin manual](https://github.com/IT-Space-ltd/TestOrchestrator/blob/main/Manuals-Doc/Admin-Manual.md)** — installation,
  configuration, workers, security, backup/upgrade, maintenance,
  troubleshooting.
- **[Screenshot index](https://github.com/IT-Space-ltd/TestOrchestrator/blob/main/Manuals-Doc/Screenshot-Doc/Screenshot-Index.md)**
  and **[validation record](https://github.com/IT-Space-ltd/TestOrchestrator/blob/main/Manuals-Doc/Validation-Record.md)** —
  screenshots were captured on the released 2.0.2 package; the record
  states which procedures were verified on which version.

## Architecture

```text
Browser ──HTTPS──> Cheroot (in-process) ──> Flask app
                                            │
   ┌ worker (toctl worker) ─ outbox/dispatch ─┤
   └ buildbot master + workers ─ execution ───┘
```

TestOrchestrator owns schedules, run IDs, portal status, reports, and
evidence; Buildbot owns queuing and the execution lifecycle behind a
loopback-only REST API (`127.0.0.1:8010`) that only the adapter
talks to. Three execution-target types are managed entirely from
**Admin → Infrastructure → Execution Targets**:

- **local** — the controller's own `to-local` Buildbot worker;
- **buildbot-worker** — a remote worker (Windows or Linux) installed
  from the bundled offline installer, connecting outbound over PB
  (per-worker credentials, no inbound portal port needed);
- **ssh** — agentless: the controller-side `to-ssh-dispatcher` worker
  runs the job over SSH/SFTP on a host that needs only SSH + the test
  runtime — no installed agent or callback listener.

- **Central server** (this package): web UI, outbox/dispatch, results,
  reporting, Buildbot master + controller workers, bundled git —
  runs on one Linux host or a Windows host.
- **Remote KRE host** (execution target): a Buildbot worker service
  installed from the bundled offline installer on the machine where
  KRE lives — or, for agentless Linux hosts, only an SSH account.
  safely encoded) — operators do not assemble raw backend flags.
  Installations are bound to a unique `kre=<label>`; remote executable
  paths are never invoked on the portal host. The internal coordinator
  credential is provisioned automatically on fresh installs
  (`ensure_coordinator_credential`, basic-auth over the coordinator
  transport — distinct from portal HTTPS), idempotent across restart /
  repair / upgrade / rerun. The Windows worker installer
  (`packaging/windows/`) targets this role.

### Reporting & dashboards

```text
SQLite ──> project_dashboard.py ──> charts.py ──> template data-fig
            (aggregation)            (Plotly fig JSON)
                                   └──> vendored plotly.js (offline)
```

- **Main Dashboard** (`/`): TestOps-inspired — period selector, KPI
  cards, outcome + duration trends, top-failing cases, projects rollup,
  recent executions; polled live updates (~5 s) with backoff and a
  stale-data badge.
- **Per-project dashboard** (`/projects/<id>/dashboard`): suite/status
  filters and four charts (outcome / pass rate / duration /
  top-failing). Aggregation stays server-side in
  `project_dashboard.py`; `charts.py` builds the Plotly figure JSON;
  the vendored `plotly.min.js` renders it — no CDN, no Dash, no second
  server. Execution counts and test-case counts are never mixed; pass
  rate excludes skips.
- **reporting-labs** (sole enhanced report backend since 2.0,
  `reporting-labs==0.1.14` vendored with its LICENSE):
  `adapters/reporting_labs.py` converts canonical
  Execution/TestResult/Artifact rows into a self-contained interactive
  report served under `sandbox allow-scripts allow-same-origin` with
  generation-scoped asset routes and bounded three-generation
  retention. The IT-Space logo is embedded as a data URI — offline and
  download-safe. A failed regeneration preserves and still serves the
  previous usable report with an explicit warning. **Historical Allure
  reports from 1.x installs are not opened or regenerated by 2.x** —
  the files are preserved on disk but unsupported by the interface.
- **Operational Logs**: structured `OperationalLog` rows (severity/
  component/category/project/execution/worker/correlation), grouped
  labelled filters, export buttons (CSV/JSONL), expandable details,
  Prev/Next pagination, live-tail status states, readable severity
  badges, redacted bounded export, independent retention — separate
  from the Audit trail.

## Supported platforms

| Platform | Status | Package |
|---|---|---|
| Ubuntu x86_64 (central server) | **2.5.0 released** | `testorchestrator-2.5.0-linux-x86_64.tar.gz` / `.run` (GitHub release assets + `SHA256SUMS.txt` + `TestOrchestrator-Buildbot-Sources-4.3.0.tar.gz`) |
| Windows amd64 (portal + worker) | **released, unsigned** | `testorchestrator-2.0.3-windows-amd64-setup-UNSIGNED.exe` |
The worker installer (`TestOrchestrator-Buildbot-Worker-Setup-`
`4.3.0-UNSIGNED.exe`) is **bundled inside the packages** — download
it from the installed portal under **Admin → Workers & runtimes**
(the matching `TestOrchestrator-Buildbot-Sources-4.3.0.tar.gz`
corresponding-source archive is offered next to it); it is not a
separate release asset.

**Ships in the package vs. installed separately:** the bundle carries
the portal (Cheroot), the Buildbot master + controller workers, the
Windows/Linux worker installers, and all Python dependencies — but
**not** Katalon Runtime Engine or a licence: install and license KRE
on each execution host separately, then register the installation in
the portal as a runtime connection. Agentless SSH targets need only
`sshd` + the runtime on the host.

The Windows executable is **unsigned** — expect SmartScreen /
publisher warnings; verify the SHA-256 below instead.

**Verification status at publication:** Linux acceptance passed on
the tested internal build (clean install, all three execution modes,
cancellation, scheduling, disconnect/recovery — see
`docs/acceptance-2.5.0.md`). Windows `.exe` acceptance runs after
release; licensed-KRE runs per target type, system-wide systemd
activation, and a separate-machine `sshd` test remain unverified —
they are tracked in the acceptance record, not marked passed.

## Installing (Linux)

```text
tar xzf testorchestrator-2.5.0-linux-x86_64.tar.gz
cd testorchestrator-2.5.0-linux-x86_64
sudo ./install.sh            # → toctl install (frozen payload)
# or: ./testorchestrator-2.5.0-linux-x86_64.run  (self-extracting)
```

`install.sh` locates its own payload, checks Linux/x86_64, and runs
`toctl install` — payload copy, data/config dirs, `web.env`,
migrations, systemd unit staging/enable — with **no external python,
pip, flask CLI, or venv** required.

Options: `--payload --data --config --https-port --frontend
--service-user --no-service-start --yes --dry-run`. Exit codes:
0 ok · 1 usage/platform · 2 install failure · 3 readiness failure.
Reruns are safe — payload is overwritten, `web.env` secrets and the
Buildbot credentials are preserved, data is never deleted.

## Installing (Windows)

Run `testorchestrator-2.0.3-windows-amd64-setup-UNSIGNED.exe`
(interactive or `/S` for silent NSIS install). It installs the frozen
portal + worker services. See the Admin Manual §5 (Windows installation, certificate
trust, service tasks, uninstall).

For a remote KRE host only, download the bundled
`TestOrchestrator-Buildbot-Worker-Setup-4.3.0-UNSIGNED.exe` from the
installed portal (**Workers & runtimes → download**, SHA-256 shown
next to the link) — a minimal worker-only install aligned to the
controller's Buildbot 4.3.0, shipped inside the app packages rather
than published standalone.

## Application commands (`toctl`)

One frozen binary implements every runtime role — no system python:

| Command | Purpose |
|---|---|
| `toctl web` | Waitress WSGI on loopback (ExecStart of the web unit) |
| `toctl worker [args]` | outbox/dispatch worker; extra args forwarded to the flask command |
| `toctl db-upgrade` | Alembic migrations to head (bundled scripts) |
| `toctl install` | full installation (see above) |
| `toctl readiness --url …` | app endpoint probe — exit 0/3 |
| `toctl backup --to DIR` | state backup (atomic `.part`→rename, retention 10) |
| `toctl recover --from F --into D` | restore (rejects corrupt archives) |
| `toctl install-info` | resolved instance/db/paths JSON |
| `toctl evidence-prune [--apply]` | dry-run report of orphan evidence dirs under managed roots; `--apply` removes them (never shared/unmanaged files) |

Services (systemd): `testorchestrator-web`, `testorchestrator-worker`,
`testorchestrator-buildbot-master`, `-buildbot-local`,
`-buildbot-ssh`. Units ExecStart the
frozen binary only. Logs: `journalctl -u testorchestrator-*`.
Persistent data (`db`, evidence, certs, backups, imported evidence)
lives outside the payload — `/var/lib/testorchestrator` by default.

## Multi-KRE execution

KRE is a **CLI engine** launched and supervised by the worker — there is
no remote KRE API. Dispatch, cancellation (process-tree kill), and
reconciliation belong to this application. KRE stays operator-installed
and licensed; a version probe is never claimed as a licensed run.

## Execution targets (Buildbot)

Every execution target is registered and managed in the Admin UI —
no controller files, commands, restarts, or Buildbot UI needed.
Administrators add, edit, test, enable, disable, and remove targets;
queued/running jobs on a disabled target are never silently
redirected. Enabled targets are selectable in both manual and
scheduled runs; run details show the actual host, execution type,
and runtime.

- **Windows Buildbot worker** — download
  `TestOrchestrator-Buildbot-Worker-Setup-<ver>-UNSIGNED.exe` from
  **System → Infrastructure** (the portal shows its version and
  SHA-256). The offline installer needs no preinstalled Python, does
  not install the controller, and bundles no KRE/licence. The admin
  creates the target record first, then enters the worker name +
  per-worker password shown once at creation. The target shows
  `Awaiting registration` until the worker connects.
- **Linux Buildbot worker** — `install_linux_worker.sh` (or the
  release equivalent) installs the worker + systemd unit; register it
  as a target the same way.
- **Agentless Linux SSH** — host/port/user, an SSH credential
  (SecretRef — values are never revealed back), remote workdir, and
  concurrency are configured in the UI. First contact requires
  explicit host-key fingerprint approval; later key changes are
  rejected until re-verified. **Test SSH Connection** stages
  reachability, host-key, auth, workdir, SFTP, and runner-prereq
  checks — which stage failed is shown without exposing secrets.
  Cancellation reports `CANCELED` only after the remote process tree
  is confirmed dead.


## Plans & scheduling

A plan = ordered suite entries with dispatch policy (sequential or
bounded-parallel), per-entry failure policy, single cancel scope, no
orchestrator-level retry (KRE `retry`/`retryStrategy` is KRE-side only).
Aggregate = **worst entry**: `failed > error > cancelled > unknown >
skipped > passed`; `not-started` is never a result; an empty plan is
`"empty"` — incomplete required work can never read as successful.
`skipped` means *no execution was ever dispatched for that entry* (a
calculated state, not a result); `not-started` means a real execution
is queued or in flight (progress, not a verdict). Retried entries count
by their latest attempt.

Implemented: plan edit/reorder/remove, per-entry `on_failure` policy
(`stop` cancels later queued/dispatched entries — the atomic launch
boundary is QUEUED|DISPATCHED → PREPARING: a cancel that commits before
the claim blocks launch; a claim that commits first means the entry
finishes (or takes CANCEL_REQUESTED — the running-process kill path is
recorded)),
rerun-failed as a new plan run (originals immutable), execution
**retry** as a linked immutable attempt (`retry_of` + incrementing
`attempt`; each attempt keeps its own workspace, report dir and
artifacts; a queued retry blocks stop-enforcement), **schedules**
(once/interval, durable table; the worker CAS-claims each due slot so
exactly one worker fires it — tested under two-contender races; fire
failure un-claims so the run is retried; a missed interval fires once
late on the next tick — no catch-up replay — and a deleted/disabled
schedule never dispatches), and **version-gated KRE options**
(allowlisted flags with declared, *provisional* minimum majors —
`retry`, `retryStrategy`, `maxFailedTests`; options the detected
runtime cannot accept **reject the launch** rather than changing the
requested semantics — explicit `kre_options_allow_drop`
acknowledgement is required to continue, recorded in `kre-gating.log`
evidence + audit).

## Importing existing results (new in 2.0.1)

Project-scoped **Import** (project detail → *Import results*) brings
external run history into the same `Execution`/`TestResult`/`Artifact`
pipeline — imported runs appear in history (filterable by
`origin=imported`), execution detail (with provenance), dashboards, and
reporting-labs like live runs.

Workflow:

1. **Preview**: point at a result root inside a project source
   repository (checked out into isolated staging — the working tree is
   never touched). Candidates are parsed, fingerprinted, and
   classified as `new`, `duplicate`, `supplement`, or `ambiguous`
   before anything is written.
2. **Confirm**: an authenticated Admin/Test Manager confirms selected
   candidates; each commits in its own transaction. Evidence is copied
   to `instance/imported-evidence/` — durable, outside the checkout.

Identity rules:

- `run_uuid` (KRE `execution.uuid`) is producer-assigned execution
  identity — unique per project when present.
- `content_hash` is **matching evidence, not identity** — two distinct
  runs with identical results stay two executions; identical content
  without UUIDs/timestamps classifies as `ambiguous`, not duplicate.
- Same UUID + identical content → duplicate (missing evidence filled).
- Same UUID + additive content (e.g. a collection suite arriving in a
  **later** import) → **supplement** — fills missing cases and
  evidence on the existing execution, never silently dropped.
- Same UUID + conflicting outcomes → `ambiguous`, requires review.
- `ambiguous` candidates support **review → force-new** to keep them
  as separate executions.
- Katalon collection runs (multiple suite report dirs sharing one
  `execution.uuid`) merge into **one** execution.
- `result_format` (`junit`) is separate from `framework`: `katalon`
  requires KRE markers (`execution.uuid`, `execution.properties`,
  `JUnit_Report.xml`, …); bare JUnit imports as `framework='unknown'`.

Safety limits: 20k dirs / 2k files / 512 MB scanned / 64 MB per XML /
500 candidates; symlinks and dotdirs skipped; `..` rejected.
Concurrent imports race-resolve through the partial unique index on
`(project_id, run_uuid)`.

## Reports & artifacts

- **Native KRE report** — retained verbatim, sandboxed view,
  `report.zip` download, idempotent refresh; never regenerated.
- **reporting-labs** (the only shipped enhanced backend) — converted +
  generated per execution from stored results: authenticated View
  (CSP sandbox, generation-scoped assets) / Download / Regenerate
  without rerunning KRE. Pure Python — no Java, no Node, no runtime
  probe needed. Works for imported executions too. Allure 3 was
  removed in 2.0; historical Allure report files are preserved but not
  openable in the interface.
- **Artifacts** — screenshots/logs/JUnit with sha256 + MIME manifest;
  `artifacts.zip` download. ReportPortal was **removed** (scope change).

## Git

All Git operations run through `git_backend.py`
(`TO_GIT_BACKEND=dulwich|cli`, default `dulwich`). The Dulwich backend is
pure Python — no GitPython, no bundled git, no host-git prerequisite:
init, clone/fetch/update (`.git` preserved for incremental sync),
exact-commit tree export, add-all/commit (incl. deletions),
conservative three-way merge (disjoint-path divergence merges;
same-path conflicts surface — never force-push), non-force push with
rejection handling, isolated `checkout` for import staging, and
branch/tag/exact-sha resolution. Verified on local/bare remotes; HTTPS
auth uses SecretRef `user:pass` secrets; SSH targets use stored
key credentials with explicit host-key approval.
HTTPS/proxy/custom-CA and Azure DevOps interoperability remain gated on
an authorized repository. The `cli` backend (bundled/host git
subprocess) stays as the opt-in fallback for source builds; selecting
it in the frozen build without a host git fails honestly, never
silently.

## Katalon project onboarding

Connect an existing Katalon repository via a project's **source** Git
binding (read-only — separate from evidence export, never pushes).
After `sync`, `gitsync.discover_project` locates `.prj` and builds a
typed inventory: cases (`Test Cases/`), suites vs collections
(**XML root tag**, not filename), profiles (names only — values never
surfaced).

**UI** — project detail: root, fetched vs discovered SHA, separate
sync/discovery status, counts, navigable items, warnings. Suite run
form: "Source definition" + "Discovered profile" selectors — type
resolves server-side from the record; `-testSuitePath` for suites,
`-testSuiteCollectionPath` for collections.

**Statuses** — `ok` (inventory current), `pending`, `none` (no `.prj`),
`failed` (prior inventory kept, marked stale — never erased).
Fetched commit and discovered commit shown separately.

**Limitations** — suite members resolve by `testCaseId` path (GUID
lookup not yet); dynamic-suite membership is flagged not computed.

## Execution controls

Run/schedule forms support explicit runner+runtime selection (auto
default; unavailable explicit picks reject — no fallback), Run-now on
saved schedules, per-schedule overlap policy (allow|skip), a
filterable/paginated `/executions` history view (project, status,
origin, framework), bulk cancel on selected rows, and imported-run
badges. See `Manuals-Doc/Validation-Record.md` for verified-vs-
claimed status.

## Browser & WebDriver selection

Suite runs and schedules take `browser` from a supported-choice
selector (Chrome / Chrome headless / Firefox / Firefox headless /
Edge / Safari) — never hard-coded; the browser runs on the **KRE
host** unless a Remote WebDriver URL is set, which emits the
Katalon-documented `-browserType=Remote` + `-remoteWebDriverUrl=` +
`-remoteWebDriverType=` form (the grid node's browser comes from the
KRE project's desired capabilities — not this URL). Edge means Edge
Chromium (all platforms); legacy Edge is Windows-only, Safari macOS —
incompatible or unknown values fail the launch rather than being
guessed. URL credentials are scrubbed from logs/evidence.
**Precedence:** the schedule or launch form's `run_config.browser`
wins → stored in the immutable execution snapshot → adapter default
`Chrome (headless)` only when unset. Plan schedules inherit the
schedule's run_config (no per-entry override exists).

## Plans, releases, and suite authoring

- **Repository-native suite authoring**: discovered suites and
  collections open in an editor (member selector, enabled flags,
  verified settings). Drafts are per-owner, record the base revision,
  and publish through a signature-bound review that checks the remote
  before writing. Publication triggers git-sync automatically.
  App suites are a labelled fallback; "Sync to repository" exports
  them into the same draft flow.
- **Releases**: scope of repository suite/collection references,
  linked executions, latest-result summary, lifecycle status
  (draft/in_progress/released/archived), optional Start/Target-end
  dates (validated `end >= start`, exported in the sync manifest),
  start→end timeline, per-release progress chart, and optional
  **Sync to repository** export (`TestOrchestration/release/<slug>.json`
  — signature-bound review, conflict-checked publish, sync-state
  tracking).
- **Schedules**: target multiple repository suites/collections via a
  two-panel selector (`name (kind) · binding · path`); each runs
  through the configured profile/runner/runtime. App suites are a
  labelled fallback. Legacy Test Plan targets are preserved as history
  but shown as unsupported — they can't be newly created. Unavailable
  targets are audited on dispatch, not silently dropped.
- **Audit**: server-side search + filters (free text, event type,
  actor, related id, date range) with 50-row pagination — Admin only.
- **Admin nav**: third-party notices & licences under Admin
  (`/about/notices`; every role still reaches it via About).

## Configuration

Integration settings live in **System → Integrations** — Buildbot
(endpoint, credential ref, auth username) and Git export (repo,
credential ref, ref) each in their own labelled section with inline
help, examples, and required/optional markers. Secrets are referenced
by name only (System → Credentials); values never enter forms or
exports. Reporting: System → Reporting. Network/ports/certs: System →
Network. Changes apply to future operations — no restart needed.

## Backup / recovery / upgrades

`toctl backup` writes `backup-*.tar.gz` atomically (`.part` → rename; a
failed write never reports success), excludes the backups dir itself,
and keeps the newest 10. `toctl recover` validates the archive before
extracting.

Upgrades are in-place: unpack the new payload over the install and run
`toctl db-upgrade` (migrations are additive; `q7r8s9t0u1v2` adds the
import schema in 2.0.1). Verified paths on v2.0.3 CI:

- **2.0.1 → 2.0.3** and **2.0.2 → 2.0.3**: dedicated jobs on Linux —
  both pass. Upgrading a 2.0.1/2.0.2 install affected by the
  reporting-labs installer defect repairs it automatically (package
  installed, superseded root-level copy removed, failed reports
  regenerable).
- **1.0.2 → 2.0.3** and **2.0.2 → 2.0.3**: dedicated jobs on Windows —
  both pass.
- Historical coverage (earlier CI): 1.0.2 → 2.0.1, 2.0.0 → 2.0.1,
  1.0.2 → 2.0.2, and 2.0.0 → 2.0.2 on both platforms. The 2.0.0-based
  jobs were retired when the v2.0.0 release was removed.

## Release builds

One command per platform — `python3 build-scripts/build_linux.py` /
`python build-scripts\build_windows.py` — fetches pinned assets,
compiles the worker installer, packages, validates, and emits
checksums + manifest into `dist/release/<platform>/`. Prerequisites
and output layout: `docs/build.md`.

## Release assets & checksums (v2.0.3)

Published at
<https://github.com/IT-Space-ltd/TestOrchestrator/releases/tag/v2.0.3>
— tag `v2.0.3` → commit `22a1400f5f17afc153a351857c3bebad47a66eac`,
all binaries built from that exact clean commit:

| Asset | Bytes | SHA-256 |
|---|---|---|
| `testorchestrator-2.0.3-linux-x86_64.tar.gz` | 278,811,947 | `2b4d97515fdc8c3e0603eaedb6f14e5fb2ce2fac9054d0157f4943e81f77b263` |
| `testorchestrator-2.0.3-linux-x86_64.run` | 278,813,951 | `7fef2b18812a17db0212f39d5f6dd7c133f9466cdbd8877ddadbb6f84bbab8de` |
| `testorchestrator-2.0.3-windows-amd64-setup-UNSIGNED.exe` | 306,850,425 | `ea7ec2fc97a4982854740001a269e7af4a5d246b3fa84412bdd417192de909a6` |
Plus `SHA256SUMS`, `build-manifest-linux.json`, and
`build-manifest-windows.json` (distinct manifest names). Verify:
`sha256sum -c SHA256SUMS` next to the downloaded files. `dist/` is
never tracked in the repo.

The `TestOrchestrator-Buildbot-Worker-Setup-4.3.0-UNSIGNED.exe` worker
installer is **bundled inside both packages** (tarball
`packaging/windows/` + inside the Windows app installer) and is
served by the installed portal — it is no longer published as a
separate release asset.

## Validation (v2.0.3)

CI on the tagged commit
([linux-package](https://github.com/IT-Space-ltd/TestOrchestrator/actions/runs/44092012798) ·
[windows-installer](https://github.com/IT-Space-ltd/TestOrchestrator/actions/runs/44092018182) —
all jobs success):

- **Linux**: build · installer-validate (real `install.sh` +
  payload-completeness gate — the job added to close the gap that hid
  the reporting-labs defect) · installed-validate · upgrade-from-2.0.1
  · upgrade-from-2.0.2.
- **Windows**: compile · installed-validate · upgrade-from-1.0.2 ·
  upgrade-from-2.0.2.
- Clean-install verification (outside the checkout, no PYTHONPATH):
  `reporting_labs` resolves from `payload/3rdparty/reporting_labs`,
  report generation produces `reporting-labs/gen-*/index.html` and
  serves it — including after restart.
- Upgrade repair verified on a seeded defective 2.0.2 install
  (`rl_status=failed`): vendored package installed automatically,
  superseded root-level copy removed, previously failed report
  regenerated, data/users/history/evidence/reports preserved.
- Package inventory reconciled: coordinator and worker both pin
  **Buildbot 4.3.0**; vendored `reporting_labs==0.1.14` + `LICENSE` +
  `VENDORED.md` present exactly once per runtime payload.
- Test suite: **637 passed, 1 skipped** — including installer
  payload-completeness, vendored-resolution, no-masking, and
  upgrade-repair regression tests.

## Deferred checks & limitations

Explicitly deferred — **not** passed:

- Windows interactive GUI walkthrough and real-corpus end-to-end
  execution — deferred (as in 2.0.0).
- Real licensed-KRE suite execution — not demonstrated (adapter is
  fixture-verified only).
- Azure DevOps repo interoperability — gated on an authorized
  repository.
- Import duplicate matching scans per-project history at classify
  time — correct at current scale; documented for index-backed lookup
  if needed.
- Non-KRE JUnit imports land as `framework='unknown'` — correct
  pending future parsers.
- KRE options gating thresholds (`retry`, `retryStrategy`,
  `maxFailedTests` minimum versions) are declared, not verified
  against a licensed KRE build.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
export FLASK_APP=wsgi:app TO_SECURE_COOKIES=0
.venv/bin/flask db upgrade && .venv/bin/flask seed-users --dev-defaults
.venv/bin/waitress-serve --listen=127.0.0.1:18080 wsgi:app
.venv/bin/python -m pytest tests/ -q
```

Bootstrap `Admin`/`Admin` forces a strong password change; headless
installs use `TO_ADMIN_PASSWORD` (≥12 chars).
