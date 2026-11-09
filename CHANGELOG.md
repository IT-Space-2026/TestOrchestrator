# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added — dashboards, reporting, operational logs, Allure 3

- **Project dashboard** (`/projects/<id>/dashboard`): per-project
  reporting with outcome/pass-rate/duration trends, top-failing-case
  ranking, summary cards, period/suite/status filters, and
  recent-executions drill-down. Aggregation in
  `project_dashboard.py`, figures via `charts.py` → vendored
  Plotly.js (offline, no CDN/Dash). Pass rate =
  passed/(passed+failed+error) with skips tracked separately;
  execution counts never mixed with test-case counts.
- **Unified chart theme** (`charts.py`): shared Plotly palette,
  base layout, and `TOCharts` client builder — the live poll now
  updates charts via `Plotly.react` instead of legacy markup.
- **Operational Logs** (`/system/logs`, admin-only): structured
  component events with severity/component/category/project/
  execution/worker/correlation fields, combined filters, bounded
  pagination, pausable live-tail with freshness indicator, and
  bounded secrets-redacted JSONL/CSV export. Independent retention
  (`logs_retention_days`/`logs_max_rows`); audit kept separate.
- **Allure 3 reporting** (replaces Allure 2): bundled Node.js +
  npm `allure` package via `allure_runtime` — no Java, no system
  node/npm/npx, no runtime downloads. Reports are multi-file
  (`awesome/` entry) served under a permitted sandbox CSP with a
  dedicated subtree-asset route; `allurerc` plugin config applies
  the Orchestrator logo + "Orchestrator — Test Report" title.
  ZIP downloads ship the whole report subtree.
- **Build payload integrity** (`verify_acquisition` +
  `verify_extracted_runtime`): pinned sha256/sha512 checks before
  extraction, explicit failures on missing/mismatched payloads —
  never silently falls back to system runtimes.
- **Main dashboard** (`/`): TestOps-inspired overview — period
  selector (7/30/90 d via `?days=`), KPI cards, side-by-side
  outcome-per-day and duration-per-day charts, top-failing-case
  table, projects rollup with pass rate, and recent executions.
  New `chart_duration` + `top_failing` aggregates in
  `dashboard.snapshot`.
- **Coordinator credential provisioning**
  (`ensure_coordinator_credential`): fresh-install generates the
  internal coordinator basic-auth password through the protected
  credential store (`kind="dagu"`, internal), wires
  `dagu_credential_ref` + `dagu_username`, idempotent — restarts,
  repairs, upgrades, and installer reruns do not regenerate or
  overwrite. `provision_dagu.py` now sources the same password via
  `TO_DAGU_PROVISION_PASSWORD` so the coordinator validates the
  credential the portal sends (previously a second random token).
- **`to-worker` launcher generation**: `worker_launcher_script()`
  produces a `.sh`/`.bat` with the coordinator address + worker
  label baked in, validated against safe identity/label regexes —
  operators run the generated artifact instead of assembling raw
  `dagu worker` flags. Admin-only download
  (`/system/infrastructure/worker-launcher/<id>/<label>`).
- **Execution source metadata**: nullable `Execution.source_ref`
  (selected binding ref at creation) + `environment` (effective
  run-config env), additive migration `m2n3o4p5q6r7`, snapshotted
  so later binding/config edits do not rewrite history. The
  existing `source_sha` remains the resolved-commit snapshot
  driving checkout.
- **Hidden-setting protection**: the normal `integrations_save`
  POST now rejects manually injected `dagu_endpoint`,
  `dagu_credential_ref`, and `dagu_username` — stored values are
  preserved, the attempt is audited, and the operator is warned.

### Changed

- **Operational Logs UI**: grouped labelled filters (severity /
  component / category / search), CSV/JSONL export as buttons,
  expandable record details, Prev/Next pagination, live-tail
  status states (live/paused/unavailable), readable white-on-color
  severity badges, and tightened table/filter alignment.
- **Allure report opens in a new tab** (`target="_blank"`
  + `rel="noopener noreferrer"`) so the portal page state is
  preserved; download actions unchanged.
- **`Admin`/`Admin` bootstrap**: on a genuinely fresh install the
  single admin starts with the shared temporary credential and
  `must_change_password`; the forced change gate is enforced until
  a strong password replaces it. Idempotent — installer reruns,
  restarts, and upgrades never reset an existing account.

### Removed

- **Allure 2 + JRE-for-Allure**: the Java `allure-commandline`
  jar, the bundled JRE shipped solely for it, `--java-home`/
  `allure-java.json` install options, `detect_java_homes`/
  `selected_java`/`java_version` helpers, `java_probe.py`, and the
  "JRE + Allure" readiness gate are gone. KRE carries its own
  runtime — the only Java consumer identified.


### Added

### Added — release dates, charts, schedule multi-targets (cont.)

- **Release dates** — nullable `start_date`/`end_date` ("Start
  date"/"Target end date") on create/edit; `end>=start` validation;
  overdue badge for unreleased/non-archived past end; `released_at`
  remains the separate actual timestamp. Dates exported in the
  release manifest + `sync_dirty` on change. `today` UTC jinja
  global for comparisons.
- **Release timeline** — Releases tab: start→end bars, status
  colors, today marker, detail links; empty state for undated
  releases. Per-release progress (latest result per scoped target,
  not all history — config-keyed, unexecuted/unavailable counted).
- **Dashboard release overview** — active (draft/in_progress)
  releases as timeline bars, undated list, overdue label, counts,
  "View all releases" — labelled current state, not date-filtered.
- **Schedule multi-targets** — `targets_json` on Schedule
  (`h8a9b0c1d2e3`): repository suites/collections referenced
  (`{kind, path, source_item_id, binding_id}`), never copied.
  Two-panel selector on create + edit (prefilled); labelled
  `name (kind) · binding · path`; App suites labelled "app-side".
- **Plan removed from scheduling** — `target_type` "plan" gone from
  create; new plan-targeted schedules rejected; existing Plan-backed
  rows show "Unsupported legacy Test Plan target"; dispatch still
  resolves preserved history.
- **Schedule dispatch** — per-target `create_execution(source_item)`
  with the schedule's profile/runner/runtime; project check on
  dispatch; unavailable targets audited (`schedule.target_unavailable`)
  not silently skipped; legacy single-target schedules still work.
- **Selector** — `{v, l}` labelled options (visible label vs
  submitted `repo:<id>` value); `esc` helper; try/catch per build;
  `js_v` cache-buster; legacy `target_id` select hidden.
- **UI** — outcome badge colors (passed/failed/running/none/
  incomplete); status-badge colors (draft/in_progress/released/
  archived); CSP-safe timeline geometry (`data-left`/`data-width`
  via JS CSSOM — inline `style=` is blocked); dashboard executions
  table outcome badges; forward-migration test stub fix.

### Migrations
- h7a8b9c0d1e2 (release start/end dates)
- h8a9b0c1d2e3 (schedule targets_json)

### Added — repository-native authoring, releases, dashboard

- **Project Dashboard** (first/default tab): stat cards (total/
  passed/failed/running/queued + pass rate), date filter, 14-day
  passed/failed trend, recent executions, inventory + repo health,
  publication pending/failed.
- **Repository Suites** — discovered suites/collections shown with
  type badges, member counts, direct Run, Edit (authoring-gated);
  read-only badge only when authoring isn't enabled.
- **Native authoring** — round-trip .ts parse/write: members + name +
  verified settings only; comments/namespaces/unknown nodes preserved;
  dynamic suites read-only. Per-owner drafts (base_sha, members,
  settings, member_cfg); review-bound publish (signature covers
  name+members+settings+cfg+destination+base); remote fetched before
  conflict check; cleanup of abandoned drafts.
- **Verified editable fields** — suite: description/tag/isRerun/
  numberOfRerun/rerunImmediately/rerunFailedTestCasesOnly/
  pageLoadTimeout; member: isRun/isReuseDriver/
  usingDataBindingAtTestSuiteLevel; collection: executionMode/
  delayBetweenInstances/maxConcurrentInstances + per-suite
  runEnabled/profileName/runConfigurationId. Preserved-not-editable:
  guid/testSuiteGuid/mailRecipient/hooks/groovy/unknown nodes.
- **Publish -> auto-sync** — a git-sync outbox event follows a
  successful publish so the new/edited definition is rediscovered
  without a manual Sync.
- **Create suite/collection** — one action each: name -> .ts path +
  member selector -> native draft -> diff -> publish. App suites are
  an explicitly labelled fallback ("not written to the repository");
  "Sync to repository" exports App suites into the same draft flow.
- **Plans removed** — routes/tab/panel/schedule targets gone;
  rows/history preserved; restored create_schedule/create_release/
  edit_release.
- **Release workflow** — detail page (scope, run-scope, status
  transitions, execution summary); scope items reference repo
  definitions (not copies); run_release queues release_id-linked
  executions with profile/runner/runtime; config-scoped latest
  results; unavailable defs blocked; released_at stamps/clears on
  reopen/restamps on re-release; exec.release link on detail.
- **Release Sync to repository** — TestOrchestration/release/<slug>.json
  (proposed app manifest): metadata + status + binding + scope refs;
  signature-bound preview (content+path+binding+base); publish
  rejects stale sig/base-tamper/upstream-advance; single/multi-binding
  guards; sync_dirty on every edit; synced_sha = last successful
  authoring commit; identical content -> no commit.
- **Schedules** — Cancel button on the create form.
- **UI** — enabled checkboxes in the case selector; status-badge
  colors (draft/in_progress/released/archived); scope-add type
  filter; css cache-buster (mtime).

### Migrations
- h3a4b5c6d7e8 (authoring drafts)
- h4a5b6c7d8e9 (draft settings/member_cfg)
- h5a6b7c8d9e0 (release_items + executions.release_id)
- h6a7b8c9d0e1 (release sync state)
- Direct execution of discovered repository suites/collections:
  /discovered/<id> detail + Run (profile/runner/runtime selects);
  executions.suite_id nullable + source_item link; durable
  source_target snapshot — identity (binding,kind,path), sha is the
  immutable revision; exactly-one-target enforced; removed/renamed/
  type-changed defs reject visibly, never retarget; source_item_id
  ON DELETE SET NULL preserves history.
- Suites emit -testSuitePath; collections -testSuiteCollectionPath.
- Report publication: TestOrchestrator/Reports/<id>/ at repo root,
  manifest v1 + assets, digest-idempotent retry, outbox-driven.
- Synchronized deletion: recorded-URL+branch commit-removal (a later
  binding change can't redirect it), never force-push/history rewrite;
  app deletion finalizes after remote; never-published deletes
  immediately; deletion-requested blocks re-publication; the UI
  confirmation states Git history is preserved.
- git_bindings.report_writable admin opt-in; tags/detached rejected.
- Single-worker report-op model via cross-process flock.
- Project page: tab navigation + collapsible forms + edit binding.
- Katalon inventory grouped by kind; suite/collection detail links.

### Migrations
- h0a1b2c3d4e5 (nullable suite_id + source_item_id)
- h1a2b3c4d5e6 (report_pub_*/deletion_* fields)
- h2a3b4c5d6e7 (source_item SET NULL + report_writable)

### UI modernization
- Control system: design tokens (spacing/radius/shadow/focus-ring,
  dark variants) applied to inputs, selects, textareas, checkboxes,
  buttons (primary/secondary/danger/ghost/icon/loading), badges,
  tables, tabs, cards, dialogs, flashes, empty states, pagination.
- Case selector (case-selector.js): two-panel available/selected —
  search, bulk add/remove visible, per-row add/remove, keyboard
  ordering, count; posts ordered case_refs[].
- Suite editor: suite_new/suite_edit/save_suite pages — grouped
  details + selector, inline field errors, binding-change warning,
  unsaved-change guard, loading submit.
- Server-side binding validation: posted case_refs validated against
  the chosen binding's worktree (foreign/partial refs rejected;
  SelectMultipleField silent-drop defect fixed).
- Multi-binding projects get an explicit binding select.

### Limitations
- Report ops serialize per-binding on a shared filesystem lock —
  same-host only; independent hosts/containers unsupported.
- Repository-target scheduling/Plans deferred.
- Claimed-event crash recovery untested.
- Studio interoperability fixture-verified only. — post-candidate fine-tuning (NOT in candidate 10300832)

### Added
- **System → Integrations** page: subsystem-separated configuration
  for Dagu (endpoint, credential ref, auth username) and Git export
  (repo path/URL, credential ref, tracked ref) — each section its own
  labelled card with per-field help (required/optional, format,
  default, restart note), inline examples, and secret-by-name guidance.

### Fixed
- **Stale frozen payload** (`docs/incidents/2026-11-04-stale-frozen-payload.md`):
  `build_bundle.py` collected a stale onedir; four revision-tagged
  candidates rejected + quarantined. Collector targets the real output
  and `check_frozen_fresh()` aborts on embedded-build mismatch
  (regression-tested).
- Atomic launch boundary: QUEUED|DISPATCHED → PREPARING CAS closes the
  claim→cancel→launch race; enforcement cancel is CAS on the row.
- Schedule CAS retries only sqlite lock/busy; unrelated DB failures
  propagate.
- KRE gated options reject the launch by default (opt-in allow-drop,
  audited); gating log registered as evidence artifact.

### Changed
- Integration settings moved out of implicit DB writes into the
  Integrations UI (keys unchanged — no migration needed).

### Added — UI overhaul (unreleased, NOT in candidate 10300832)
- **Operational dashboard** replaces the welcome page: live metric
  cards (visible projects, active/queued executions, passed/failed
  terminal attempts in a 14-day window, dispatch-eligible/healthy
  runners, upcoming schedules), per-project table (latest outcome,
  active work, pass rate over terminal *attempts*, next scheduled run),
  recent-executions table, and a daily-outcomes bar chart — all real
  data, honest empty states.
- **Live updates:** `GET /dashboard/data` polled ≈ every 5 s
  (near-real-time) — single in-flight request, backoff to 60 s on
  errors, paused on hidden tabs, stale-data badge, in-place DOM patch
  preserving filters/scroll. Same project-visibility scoping as the
  page.
- **Projects page:** card grid with search (name/description),
  sort (name / latest activity), outcome badges, active-work and
  execution counts, clear empty/no-results states.
- **Audit:** server-side search + filters (free text over event/
  detail/actor, event type, actor, related id, date range) with
  pagination (50/page), preserved-filter links, deterministic ordering.
- **Runners & runtime forms:** per-field help (required/optional,
  format, runner-vs-host path semantics, credential references).
- **Nav:** third-party notices moved to Admin → Third-party notices
  & licences; URL unchanged (/about/notices) and still reachable by
  every role via About.

- **Browser selector:** supported-choice dropdown (suite run +
  schedule forms) replaces the hard-coded Chrome default; remote
  WebDriver URL/type fields override -browserType; platform validation
  (Edge=Windows, Safari=macOS); choice persisted in the snapshot and
  shown on the execution page. Scheduled Firefox verified end-to-end
  as `-browserType=Firefox` on the fixture KRE.

- **Remote execution safety:** atomic assignment CAS (capacity +
  ownership) on all paths; `POST /api/agent/tasks/<id>/claim` pickup
  CAS; agent passes timeout_seconds + remote-webdriver config;
  UNKNOWN executions hold capacity until admin resolves them via the
  new **Resolve UNKNOWN** action (audited); multi-process race tests.
  docs/execution-capability.md + docs/runbook-two-host.md.

- **Execution controls (Legacy-parity round):** explicit runner/
  runtime selectors (auto default, revalidated at dispatch, no
  fallback); Run now on schedules (saved config, recurrence
  preserved); per-schedule overlap policy (allow|skip, UNKNOWN
  blocks); searchable/paginated /executions history with filters;
  bulk cancel on selected rows; executions.schedule_id linkage
  (migration e6f7a8b9c0d1).

- **Katalon project onboarding:** `gitsync.discover_project` —
  .prj root detection (bounded, containment-checked), typed
  DiscoveredItem inventory (cases/suites/collections/profiles by
  XML root tag), ordered membership refs, atomic replace per SHA,
  stale-preserving failures, inventory UI section, suite-vs-collection
  execution args (-testSuiteCollectionPath). Migrations
  f8a9b0c1d2e3 + g9a0b1c2d3e4.

### Fixed (unreleased)
- **Remote WebDriver contract:** corrected to Katalon's documented
  `-browserType=Remote` + `-remoteWebDriverUrl=` +
  `-remoteWebDriverType=` (was dropping -browserType); URL credential
  scrubbing; grid-browser selection documented as desired-capabilities
  driven, not URL-driven. See docs/execution-capability.md.

### Known limitations
- `CANCEL_REQUESTED` running-process kill path recorded but not yet
  verified on the frozen worker.
- Root/systemd, licensed-KRE, second-host worker, Azure DevOps and
  Windows remain unvalidated environment gates.
