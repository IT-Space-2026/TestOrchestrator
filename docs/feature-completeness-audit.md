# Feature-completeness audit — TestOrchestrator 1.9.0 (commit 99014c1)

Legend: **DONE** implemented+tested · **PARTIAL** implemented/incomplete
evidence · **ABSENT** not built · **GATE** requires real-host/3rd-party
evidence. "Evidence" separates unit/integration tests from live-target
proof. Orchestrator-supplied features are distinguished from what only
exists inside an Allure report or a ReportPortal instance.

## Capability matrix

| Capability | Status | UI/API + source | Tests / evidence | Limitations & remaining work |
|---|---|---|---|---|
| Project & repository setup | DONE | Portal → Projects (`portal/routes.py`, `execution.py:Project`, `GitBinding`) | test_infra: binding create/sync, clone at recorded SHA | HTTPS/SSH remote creds tested with file:// only — real remote is a GATE |
| Git sync (clone/push/export/restore/conflict) | DONE | `gitsync.py`, `git_runtime.py` (vendored git, child-scoped env) | test_gitsync_remote: roundtrip, conflict no-force-push, secret exclusion | Remote-provider behaviour = GATE |
| Suite/profile selection & execution config | DONE | Portal → Suites/Plans (`Suite`, `SuiteEntry`, `Plan`, run profiles) | suite CRUD, immutable snapshots, entry add bumps version | Rich builder UI (ordering UX, hooks) PARTIAL |
| Immediate execution | DONE | Execution dispatch (`dispatch.py`, adapters/kre.py) | test_pipeline, fixture-KRE chain proven | **Real KRE = GATE** (connect-only adapter is fixture-verified) |
| Recurring schedules & schedule mgmt | PARTIAL | Dagu orchestration supports scheduling; no dedicated schedule CRUD UI | dagu contract test vs bundled 2.25.2 | Schedule create/pause/edit UI + evidence needed |
| Worker registration/readiness/dispatch | DONE | Admin → Infrastructure: Runner, RuntimeConnection, probe, drain, agent token issue (`system/routes.py`, `agent_api.py`) | token sha256-persisted, probe, drain tests | Remote agent on a real second host = GATE (API is in-process tested) |
| Cancellation | DONE | POST `/executions/<id>/cancel` → process-tree kill → CANCELED | test_pipeline cancel tests | — |
| Reconnect/restart recovery | PARTIAL | lease expiry → UNKNOWN, never re-dispatch; reconcile loop | dagu reconcile tests | Live disconnect/reconnect on real hosts = GATE |
| Run history & filtering | PARTIAL | Portal → Executions list + detail | route tests | Rich filtering/search/pagination = remaining work |
| Summaries & case drill-down | DONE | execution detail: outcome, per-case results (TestResult), logs | results parser tests | — |
| Native KRE reporting | DONE | report assess ready/partial/missing/failed; view (CSP sandbox), `report.zip`, refresh idempotent (`reporting.py`) | test_reporting | never re-runs KRE for a report |
| Allure reports | DONE | adapter convert+generate, atomic publish, single-file, view+zip+regen (`adapters/allure.py`, `allure_runtime.py`) | test_allure_reporting: real generation via host Java 21 | requires external validated Java (new prerequisite model) |
| Screenshots/logs/artifacts | DONE | `Artifact` registry, evidence manifest sha256+MIME, `artifacts.zip`, per-artifact download | test_reporting, test_results_parser | — |
| Original-artifact downloads | DONE | `artifacts.zip` = original evidence + checksum manifest | test_reporting | distinguished from regenerated reports |
| ReportPortal config & optional publication | DONE | Admin → Reporting (endpoint/project/cred-ref/TLS/CA/test launch); per-execution pin, AMBIGUOUS state (`adapters/reportportal.py`) | test_reportportal: pin-block, ambiguity, retry semantics | Live RP publish = GATE (candidate compose file only) |

## Report/export formats (generated vs retained)

| Format | Produced by Orchestrator? | Notes |
|---|---|---|
| Native KRE report (HTML view + report.zip) | retained+packaged | original KRE output, never regenerated |
| Allure single-file HTML | **generated** | needs external Java, validated probe |
| artifacts.zip (original evidence + manifest) | packaged | originals retained verbatim |
| JUnit results (canonical normalized) | generated from KRE output | feeds cases/Allure/RP |
| ReportPortal launch | **generated** remotely | optional; orchestrator-side config only |
| Trends/comparison/flaky exports | ABSENT | out of scope for first release |

## Out-of-scope (explicitly declared, not silently missing)

Cross-run history/trends, run comparisons, flaky-test analysis,
notification channels (mail/chat/webhooks), defect/tracker integrations
(Jira etc.) — **none implemented; declared future enhancements, not
first-release requirements**. Allure-internal widgets (trend, history
inside the generated report) are report-content, not Orchestrator
features.

## Platform/infrastructure

| Area | Status | Evidence | Limitations |
|---|---|---|---|
| Authorization & roles | DONE | bootstrap Admin→mandatory change, Argon2id, 4 roles, session revoke, rate limit, last-Admin protection | test_infra/test_bootstrap |
| Secrets store | DONE | Fernet, key outside DB, refs never render values | test_infra |
| Backup/recovery | DONE (fixed) | updater backup/restore/checkpoint; self-inclusion bug FIXED + regression tests + retention(10) + atomic .part + corrupt-archive rejection | test_backup_regressions |
| Migrations | DONE | Alembic multi-head merged → single head `2296aff581c6`; clean `flask db upgrade` verified | upgrade from prior release = GATE |
| Linux installer | PARTIAL→implemented | `install.sh` bootstrap + `scripts/install.py`: venv offline, dagu, java probe, service enable/start, readiness, idempotent rerun, exit codes | smoke-tested in temp dirs (non-root); clean-host + reboot = GATE |
| Windows worker installer | PARTIAL | NSIS scaffold + payload assembly + unattended contract (`packaging/windows/`) | no makensis build, no Windows host evidence — all GATE |
| Updater apply pipeline | PARTIAL | verify/digest/lock/stage/checkpoint/resume functions + tests | end-to-end apply+activate+health chain untested |

## First-release vs optional

**Agreed first-release:** all DONE rows above + Linux install.sh +
Windows worker installer (target evidence pending) + Java external
prerequisite model.
**Optional future:** trends/flaky/notifications/defect integrations,
schedule CRUD UI, Windows/SLES server roles, RP stack bundling.
