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

### Known limitations
- `CANCEL_REQUESTED` running-process kill path recorded but not yet
  verified on the frozen worker.
- Root/systemd, licensed-KRE, second-host worker, Azure DevOps and
  Windows remain unvalidated environment gates.
