# TestOrchestrator

<p align="center"><img src="assets/branding/iT-space-logo-400x400.png" alt="IT-Space Kft. logo" width="120"></p>

Test orchestration web application for **IT-Space Kft.** — Flask + Jinja2
server-rendered UI, Waitress WSGI, Dagu workflow orchestration, Katalon
Runtime Engine (KRE) execution via workers, and Allure reporting behind
an externally supplied Java runtime.

**Status: prerelease (v1.9.0).** Implementation is ahead of acceptance —
see [Acceptance gates](#acceptance-gates). Release assets ship as a GitHub
prerelease, never as committed archives.

## Architecture

```text
Browser ──HTTPS──> Caddy ──loopback HTTP──> Waitress ──WSGI──> Flask app
                                            │
   ┌ worker (toctl worker) ─ outbox/dispatch ─┤
   └ dagu (orchestration)  ─ execution runs ──┘
```

Two roles:

- **Central server** (this package): web UI, outbox/dispatch, results,
  reporting, Dagu, Caddy, bundled git — runs on one Linux host.
- **Remote KRE host** (Dagu worker): a pinned **Dagu worker** service
  runs on the machine where KRE is installed, connects outbound to the
  portal's Dagu coordinator (no inbound port needed), and executes the
  registered KRE there. Installations bound to a unique
  `--worker.labels kre=<label>`; remote executable paths are never
  invoked on the portal host.
  The Windows worker installer (`packaging/windows/`) targets this role —
  **scaffold only; not yet built or host-validated**.

## Supported platforms (evidence level)

| Platform | Status | Evidence |
|---|---|---|
| Ubuntu x86_64 (central) | **prerelease** | install.sh → toctl chain verified from fresh extraction; clean-host install + reboot pending |
| Windows Server 2025 (worker) | **scaffold** | NSIS source + unattended contract; no .exe built, no host test |
| Windows 11 / SLES | **not packaged** | out of current scope |

## Installing (Linux)

```text
tar xzf testorchestrator-<ver>-linux-x86_64.tar.gz
cd testorchestrator-<ver>-linux-x86_64
sudo ./install.sh            # → toctl install (frozen candidate) or
                             #    bundled-python install.py (standard build)
```

`install.sh` locates its own payload, checks Linux/x86_64, and dispatches:

- **Frozen candidate** (`app/toctl` present): `toctl install` performs the
  entire install — payload copy, data/config dirs, `web.env`, migrations,
  Java detection + real Allure probe, systemd unit staging/enable —
  **no external python, pip, flask CLI, or venv**.
- **Standard build**: bundled CPython + offline wheel install
  (`scripts/install.py`) — the working fallback until the frozen
  candidate passes acceptance.

Options: `--payload --data --config --https-port --service-user
--java-home --no-allure --no-service-start --yes --dry-run`. Exit codes:
0 ok · 1 usage/platform · 2 install failure · 3 readiness failure. Reruns
are safe — payload is overwritten, `web.env` keys are replaced, data is
never deleted.

> Applies to source commit ≥ `94730a8` / prerelease `v1.9.0` assets.
> Earlier assets use the venv installer path only.

## Application commands (`toctl`)

One frozen binary implements every runtime role — no system python:

| Command | Purpose |
|---|---|
| `toctl web` | Waitress WSGI on loopback (ExecStart of the web unit) |
| `toctl worker [args]` | outbox/dispatch worker; extra args forwarded to the flask command |
| `toctl db-upgrade` | Alembic migrations to head (bundled scripts) |
| `toctl install` | full installation (see above) |
| `toctl readiness --url …` | app endpoint probe — exit 0/3 |
| `toctl java-probe` | Java detect + real Allure probe → JSON |
| `toctl backup --to DIR` | state backup (atomic `.part`→rename, retention 10) |
| `toctl recover --from F --into D` | restore (rejects corrupt archives) |
| `toctl install-info` | resolved instance/db/paths JSON |

Services (systemd): `testorchestrator-web`, `testorchestrator-worker`,
`testorchestrator-dagu`, `testorchestrator-caddy`. Units ExecStart the
frozen binary only. Logs: `journalctl -u testorchestrator-*`.
Persistent data (`db`, evidence, certs, backups) lives outside the
payload — `/var/lib/testorchestrator` by default.

## Multi-KRE execution

KRE is a **CLI engine** launched and supervised by the worker — there is
no remote KRE API. Dispatch, cancellation (process-tree kill), and
reconciliation belong to this application. KRE stays operator-installed
and licensed; a version probe is never claimed as a licensed run.

## Remote execution (Dagu worker)

A remote KRE installation runs through the bundled, pinned **Dagu
2.25.8** coordinator/worker pair — never through a portal-host
invocation and never through a custom agent.

Setup sequence:

1. Install KRE on the test computer (operator-installed + licensed —
   KRE is not redistributed).
2. Download the Dagu worker archive for that computer's platform from
   **System → KRE installations → Download Dagu worker** (bundled,
   checksum-verified Linux amd64 + Windows amd64 assets; SHA-256 is
   published next to each download).
3. On the KRE computer, start the worker pointing at the portal's
   coordinator address — **not** `localhost` for a different machine:

   ```bash
   ./dagu worker --worker.coordinators=<portal-host>:50055 \
     --worker.labels kre=<unique-label> --worker.id <worker-name>
   ```

   The worker polls outbound over coordinator gRPC; no inbound port is
   required on the KRE machine.
4. Register the installation in the portal as **On another machine**
   with the same `kre=<label>`; labels must be unique per installation
   (dispatch rejects ambiguous or unmatched labels).
5. Verify and execute.

Delivery + lifecycle: each execution is submitted as an ad-hoc workflow
spec with `dagRunId` pinned to the execution id (idempotent, singleton),
routed by `workerSelector` to the labelled worker, which
`git.checkout`s the recorded source revision into a per-execution
workspace and runs the registered executable with reports written to
the run's artifact store. The portal downloads artifacts and runs the
same collect/normalize pipeline as local execution. Cancellation uses
HTTP stop; a worker loss/shutdown abort is recorded as infrastructure
failure with cause marked undetermined — never as user cancellation.
Workspaces are cleaned via a bounded portal-driven cleanup run, not
the workflow's exit handler (which does not run on abort).

Security boundaries: HTTPS git credentials are **not** embedded in
stored workflow specs — remote dispatch accepts unauthenticated
remotes and worker-side `ssh_key_path` only. `ssh_passphrase` is a
literal value field — do not pass real passphrases through the spec;
prefer worker-local keys + ssh-agent with operator-managed host-key
verification. The worker account needs only: read access to the
checkout destination, execute on the KRE binary, write under the
workspace root, and outbound gRPC to the coordinator.

- Multiple workers register per-project with probe/drain/lease handling.
- **Slots vs licences are separate limits**: `Runner.max_slots` is
  hardware; `Runner.license_slots` is an **operator-declared per-worker
  floating-seat allowance**. Dispatch ceiling = `min(slots, seats)` —
  seats are never probed or assumed. First release is scoped to
  **operator-partitioned allowances** — there is no shared-pool
  enforcement and no visibility into sessions started outside the app.
- Readiness ladder: worker reachable → KRE probeable → prereqs →
  licence/auth → approved execution. Real licensed-KRE execution is an
  open acceptance gate (fixture-verified adapter only so far).

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
recorded but **not** yet verified on the frozen worker). Verified under
synchronized claim→cancel→launch races),
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
evidence + audit; thresholds are declared, not verified against a
licensed KRE build).

## Reports & artifacts

- **Native KRE report** — retained verbatim, sandboxed view,
  `report.zip` download, idempotent refresh; never regenerated.
- **Allure** (optional, currently the only shipped enhanced backend;
  **pyrept** evaluated as a candidate alternative — MIT, Java-free,
  allure-results + JUnit import verified on fixtures: outcomes,
  durations, failure traces and suite names convert; retries fold by
  `historyId` into `retries`/`flaky` (individual attempt detail lost);
  **attachments/screenshots are NOT imported** and KRE-native reports
  are not its input path — it is not yet a replacement) — converted +
  generated
  per execution: authenticated View (CSP sandbox) / Download /
  Regenerate without rerunning KRE. Requires a validated **external**
  Java runtime (admin-selected path → `JAVA_HOME` → `PATH` → platform
  locations; probe runs a real pinned-Allure generation, persisted and
  re-checkable in Admin → Reporting). Missing Java disables Allure only.
- **Artifacts** — screenshots/logs/JUnit with sha256 + MIME manifest;
  `artifacts.zip` download. ReportPortal was **removed** (scope change).

## Git

All Git operations run through `git_backend.py`
(`TO_GIT_BACKEND=dulwich|cli`, default `dulwich`). The Dulwich backend is
pure Python — no GitPython, no bundled git in the frozen candidate, no
host-git prerequisite: init, clone/fetch/update (`.git` preserved for
incremental sync), exact-commit tree export, add-all/commit (incl.
deletions), conservative three-way merge (disjoint-path divergence
merges; same-path conflicts surface — never force-push), non-force push
with rejection handling, and branch/tag/exact-sha resolution. Verified
on local/bare remotes; HTTPS auth uses SecretRef `user:pass` secrets;
SSH uses host ssh/agent. HTTPS/proxy/custom-CA and Azure DevOps
interoperability remain gated on an authorized repository. The `cli`
backend (bundled/host git subprocess) stays as the opt-in fallback for
source builds; selecting it in the frozen build without a host git
fails honestly, never silently.

## Backup / recovery / upgrades

`toctl backup` writes `backup-*.tar.gz` atomically (`.part` → rename; a
failed write never reports success), excludes the backups dir itself,
and keeps the newest 10. `toctl recover` validates the archive before
extracting. Updates stage through the signed-bundle verifier; rollback
uses the last good backup.

## Release assets & checksums

GitHub prerelease `v1.9.0`: `tar.gz` + `.sha256` + `.manifest.json` +
`.inventory.json` (release notes in the release body). `dist/` is never
tracked. Standard build ≈179 MB; frozen candidate ≈144 MB (GitPython
and the bundled git rootfs removed — Dulwich-only). Revision-tagged
names (`-frozen-<shortsha>`); `release.json` inside carries the build
id the binary reports via `/healthz` and `toctl install-info`.
`build_bundle --frozen` aborts if the onedir's embedded build differs
from the repo stamp (stale-payload guard). Verify:
`sha256sum -c <file>.sha256` inside `dist/`.

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

## Acceptance gates (open, honestly not yet demonstrated)

Demonstrated on the loopback fixture (Dagu 2.25.8, isolated portal):
worker dispatch → git.checkout → fixture KRE → artifact return →
normalize → execution detail; failed-test evidence; explicit cancel
(process tree killed); graceful worker shutdown → honest aborted
classification (infrastructure, not user cancel); worker reconnect.

Still open: clean-host install + reboot health · real licensed-KRE
suite · hard worker crash / network partition behavior · Windows
Server 2025 installer build/acceptance · Azure DevOps repo
interoperability · root/systemd service-account install path (blocked
on this build host: no sudo).

## Configuration (Unreleased fine-tuning)

Integration settings live in **System → Integrations** — Dagu
(endpoint, credential ref, auth username) and Git export (repo,
credential ref, ref) each in their own labelled section with inline
help, examples, and required/optional markers. Secrets are referenced
by name only (System → Credentials); values never enter forms or
exports. Allure: System → Reporting. Network/ports/certs: System →
Network. Changes apply to future operations — no restart needed. This
section post-dates candidate `10300832` (unreleased).

## Dashboard & UI (Unreleased)

The project Dashboard is the first/default tab: execution stat cards
(total/passed/failed/running/queued + pass rate), a date filter,
14-day passed/failed trend, recent executions, inventory and repo
health, and report-publication pending/failed state.

## Repository-native suite authoring (Unreleased)

Repository suites and collections are first-class: discovered
definitions open in a modern editor (member selector, enabled flags,
verified suite/collection settings). Drafts are per-owner, record the
base revision, and publish through a signature-bound review that
checks the remote before writing. Publication automatically triggers
a git-sync so the definition is rediscovered without a manual Sync.
App suites are a labelled fallback; "Sync to repository" exports them
into the same draft flow.

## Releases (Unreleased)

Releases carry optional Start date / Target end date (validated
`end >= start`, shown on list/detail/edit, exported in the sync
manifest). The Releases tab has a start→end timeline and a
per-release testing-progress chart (latest result per scoped
target, not all history). The Dashboard shows a compact active-
release overview with overdue/undated counts.

## Schedules (Unreleased)

Schedules target multiple repository suites/collections via a
two-panel selector (`name (kind) · binding · path` labels); each
runs through the configured profile/runner/runtime. App suites are
a labelled fallback. Legacy Test Plan targets are preserved as
history but shown as unsupported — they can't be newly created.
Unavailable targets are audited on dispatch, not silently dropped.

## Releases (Unreleased)

A release carries a testing scope of repository suite/collection
references, linked executions (profile/runner/runtime), a config-
scoped latest-result summary, lifecycle status (draft/in_progress/
released/archived), and an optional **Sync to repository** export
(TestOrchestration/release/<slug>.json — a proposed app manifest,
signature-bound review, conflict-checked publish, sync-state
tracking).

- **Dashboard:** real-data metric cards (visible projects, active/
  queued executions, passed/failed terminal attempts over 14 days —
  each attempt counts once, reruns are separate attempts; dispatch-
  eligible vs healthy runners; upcoming scheduled runs), per-project
  table (latest outcome, active work, pass rate over terminal attempts,
  next schedule), recent executions, and a daily-outcomes bar chart.
- **Live updates:** `GET /dashboard/data` polled ≈ every 5 s
  (near-real-time) — one in-flight request, exponential backoff to
  60 s on errors, paused on hidden tabs, stale-data badge; DOM patched
  in place (filters/scroll preserved). Same project-visibility scope
  as the page.
- **Projects:** card grid — search (name/description), sort
  (name / latest activity), outcome badges, active-work counts,
  clear empty states.
- **Audit:** server-side search + filters (free text, event type,
  actor, related id, date range) with 50-row pagination preserving
  filters, deterministic ordering — Admin only.
- **Runners & runtimes:** per-field help (required/optional, format,
  runner-host path semantics, credential-by-name references).
- **Admin nav:** third-party notices & licences moved under Admin
  (URL `/about/notices` unchanged; every role still reaches it via
  About).

## Browser & WebDriver selection (Unreleased)

Suite runs and schedules take `browser` from a supported-choice
selector (Chrome / Chrome headless / Firefox / Firefox headless /
Edge / Safari) — never hard-coded; the browser runs on the **KRE
host** unless a Remote WebDriver URL is set, which emits the
Katalon-documented `-browserType=Remote` + `-remoteWebDriverUrl=` +
`-remoteWebDriverType=` form (the grid node's browser comes from the
KRE project's desired capabilities — not this URL). Edge means Edge
Chromium (all platforms); legacy Edge is Windows-only, Safari macOS —
incompatible or unknown values fail the launch rather than being
guessed. URL credentials are scrubbed from logs/evidence. **Precedence:** the schedule or launch form's
`run_config.browser` wins → stored in the immutable execution snapshot
→ adapter default `Chrome (headless)` only when unset. Plan schedules
inherit the schedule's run_config (no per-entry override exists).

## Execution controls (Unreleased)

Run/schedule forms support explicit runner+runtime selection (auto
default; unavailable explicit picks reject — no fallback), Run-now on
saved schedules, per-schedule overlap policy (allow|skip), a
filterable/paginated `/executions` history view, and bulk cancel on
selected rows. See `docs/legacy-testops-gap-audit.md` and
`docs/execution-capability.md` §10.

## Katalon project onboarding (Unreleased)

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

**Migrations** — `f8a9b0c1d2e3` (discovery schema),
`g9a0b1c2d3e4` (`members_json` forward path). Run `db-upgrade` before
the UI; an un-upgraded DB shows a missing-column error.

**Limitations** — suite members resolve by `testCaseId` path (GUID
lookup not yet); dynamic-suite membership is flagged not computed;
schedule/plan sources still use suite-list execution; concurrent-
discovery ordering untested.
