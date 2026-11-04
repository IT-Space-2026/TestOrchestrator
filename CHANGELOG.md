# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — post-candidate fine-tuning (NOT in candidate 10300832)

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
