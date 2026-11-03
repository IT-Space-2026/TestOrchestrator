# Implementation status inventory

Date: 2026-11-03. Required by handoff §3. Statuses: **implemented+tested** /
**implemented, unverified** / **partial** / **stub** / **missing** /
**blocked**. Nothing below claims success without executable evidence (A14).

## Working and tested (pytest suite)

| Capability | Files | Tests | Status |
|---|---|---|---|
| v1.7 About + IT-Space branding (all roles, offline, notices, admin changelog) | `testorchestrator/main/routes.py`, `templates/main/_about_content.html`, `static/js/about.js` | `test_about.py` (AB01–AB07) | implemented+tested — official logo still owner-blocked (AB07) |
| v1.8 bootstrap Admin/Admin + mandatory change + loopback gate + rate limit | `bootstrap.py`, `auth/routes.py` | `test_bootstrap.py` (BOOT01–04) | implemented+tested |
| Address model, ports (443/8080/18080), redirect-only HTTP rendering | `network.py`, `caddyconf.py`, `settings.py` | `test_network.py` (NET01–05) | implemented+tested |
| Certificate generate/import, SAN/expiry/chain checks, protected keys | `certs.py`, `system/routes.py` | `test_certs.py` (CERT01–04) | implemented+tested |
| Four roles, Argon2id, CSRF, secure sessions, session revocation, last-Admin protection | `models.py`, `auth/`, `system/routes.py` | `test_infra.py`, `test_security.py` | implemented+tested |
| Admin user management (create/reset/deactivate) | `system/routes.py`, `templates/system/users.html` | `test_infra.py` | implemented+tested |
| Fernet secret store (key outside DB, rotation metadata, not-configured errors) | `secrets_store.py` | `test_infra.py` | implemented+tested |
| Canonical JUnit parser + outcome semantics (skip≠pass, no zero-test pass) | `results.py` | `test_results_parser.py` | implemented+tested |
| Evidence manifest (checksums, MIME, active-content flags, path safety) | `results.py`, `portal/routes.py` | `test_results_parser.py`, `test_infra.py` | implemented+tested |
| Immutable execution snapshots, independent state fields | `execution.py`, `dispatch.py` | `test_pipeline.py` | implemented+tested |
| Local runner: capacity, drain, leases, reconciliation to UNKNOWN | `dispatch.py`, `adapters/runner.py` | `test_pipeline.py` | implemented+tested |
| Process-tree cancellation via process groups | `adapters/kre.py`, `dispatch.py` | `test_pipeline.py::test_cancel_kills_process_tree` | implemented+tested |
| Durable outbox + `flask worker` (dispatch outside request handlers) | `execution.py`, `dispatch.py`, `__init__.py` | `test_pipeline.py` | implemented+tested |
| Git export `TestOrchestrator/` layout, whitelist kinds, checksum verify | `gitsync.py` | `test_infra.py::test_git_export_layout` | implemented+tested (local repo paths) |
| Git binding clone/index, read-only case/suite listing | `gitsync.py` | covered by local-path clone path | implemented+tested for local/file URLs; HTTPS/SSH auth paths unverified |
| Portal UI: projects/bindings/suites/plans/releases/executions/artifacts | `portal/`, `templates/portal/` | `test_infra.py::test_project_scope_enforced` | implemented+tested |

## Implemented but unverified (requires operator-supplied services)

| Capability | Files | Blocker |
|---|---|---|
| KRE CLI compatibility (argument spelling, exit meanings, version probes) | `adapters/kre.py` | needs real operator-supplied KRE executable; fixture proves mechanics only |
| Dagu adapter (health/submit/status/logs/cancel) | `adapters/dagu.py` | needs live Dagu instance + pinned version verification |
| ReportPortal adapter (launch/items/logs/attachments/finish) | `adapters/reportportal.py` | needs live ReportPortal instance; A02 screenshot mapping unproven |
| HTTPS/SSH Git credential transports | `gitsync.py`, `secrets_store.py` | needs real remote repos + credentials |

## Partial

| Capability | Gap |
|---|---|
| Remote runner transport | architecture decision made (secured Python agent, §6 option A); not implemented — remote runners never get assigned |
| Plan parallelism | plan entries queue sequentially; bounded parallelism beyond capacity selection not implemented |
| Evidence→case mapping | screenshots attach per `result_id` when mapped; filename association not attempted (per §9); richer manifest mapping pending |
| Releases | native CRUD + mark-released + plan links; no run-from-release reports view yet |
| `development-insecure` HTTP mode | selectable but renderer emits no listener — needs explicit approval gate |

## Missing / blocked

| Capability | Blocker |
|---|---|
| Real KRE execution evidence (A01, A02) | operator inputs: installed KRE, licence, browsers, project |
| Installer (§13), updater/recovery flow (§14) | not implemented; deploy assets only |
| PQ transport verification (§15) | needs bundled-Caddy TLS negotiation testing with supported clients |
| Remote agent, containerized ReportPortal baseline | platform decisions + infrastructure |

## Acceptance A01–A14

- **A01, A02, A04**: blocked on operator KRE/Dagu/ReportPortal inputs.
- **A03**: implemented — local canonical results authoritative; RP/Git failures recorded independently, no rerun.
- **A05, A06**: tested locally (cancel tree kill, lease reconciliation to UNKNOWN).
- **A07**: partial — snapshot pins SHA per binding; multi-binding plan resolution exists only as one binding per project today.
- **A08**: partial — export writes layout; full restore-from-clone not implemented.
- **A09, A10**: blocked — installer/updater not implemented.
- **A11**: partial — four roles enforced server-side incl. artifact paths; full matrix test pending.
- **A12**: tested (BOOT02/04 + reset/self-service).
- **A13**: partial — cert import works; PQ gate explicitly open.
- **A14**: enforced — unverified integrations are marked so, here and in code.
