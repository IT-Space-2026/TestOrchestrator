# TestOrchestrator changelog

## 1.9.0 — 2026-11-03

- Handoff: execution pipeline foundation — immutable execution snapshots,
  durable outbox (`flask worker`), local runner with capacity/drain/leases,
  process-tree cancellation, reconciliation to UNKNOWN (never re-dispatch).
- Handoff: connect-only KRE adapter (argv invocation, isolated workspace at
  recorded SHA, report discovery, error classification) — CLI verified vs.
  fixture only, real KRE pending operator input.
- Handoff: canonical JUnit results + evidence manifest (checksums, MIME,
  active-content policy), independent state fields
  (execution/outcome/RP/Git).
- Handoff: Dagu and ReportPortal versioned REST clients — implemented but
  unverified, honestly marked; not-configured is actionable, not faked.
- Handoff: GitPython persistence — source binding clone/index + destination
  export under `TestOrchestrator/` layout with evidence whitelist.
- Handoff: Fernet credential store (key outside DB), Admin user management
  (create/reset/deactivate, session revocation, last-Admin protection),
  runners & KRE runtime registration/probe UI, project-scoped portal
  (projects/bindings/suites/plans/releases/executions/artifacts).
- Docs: implementation-status, integration-contracts, compatibility
  inventory per handoff section 3.
- Packaging: bundled JRE removed from the distribution (and unpublished
  Git history) — Java is an operator prerequisite for Allure reporting:
  installer detects/selects (`--java-home`), validates via a real
  report-generation probe as the service account, persists
  `TO_ALLURE_JAVA_HOME` scoped to the app services, or continues with
  Allure disabled (`--no-allure`) + warning. Admin → Reporting shows
  Java path/version/readiness and re-validates on change. No global
  Java settings change; KRE runtime untouched; `dist/` kept out of Git.

## 1.8.0 — 2026-11-03

- v1.8: bootstrap Admin (Admin/Admin, Argon2id, mandatory first-login
  password change with restricted session, rate limiting, durable
  setup_completed, session rotation); loopback-only access until setup is
  complete and external access is explicitly enabled; TO_ADMIN_PASSWORD
  installer path.
- v1.8: address model — local https://localhost:443, optional external/LAN
  host (IP or DNS) with detected non-loopback candidates requiring explicit
  choice; read-only computed access links; host allowlist.
- v1.8: ports — public HTTPS 443, public HTTP 8080 (redirect-only to the
  configured HTTPS address), internal Waitress moved to 127.0.0.1:18080;
  integer/conflict/privilege validation; staged Caddy render + validate +
  apply with previous_good rollback.
- v1.8: certificate setup — per-install local CA generation and PEM
  chain/key import (key match, SAN coverage, validity, chain completeness)
  via the cryptography library; protected key storage (0700/0600); Admin ->
  System -> Network & certificates UI.

## 1.7.0 — 2026-11-02

- v1.7: About dialog/page for all authenticated roles (Admin, Test Manager,
  Tester, PO) with IT-Space Kft. company branding, HTTPS homepage link,
  bundled version/build metadata, and local third-party notices.
- v1.7: temporary placeholder branding pending owner-approved official logo
  (tracked as incomplete production requirement, AB07).
- Foundation: Flask + Jinja2 application shell, Argon2id credential store,
  role model, audit log, admin System pages (Changelog, Audit), Waitress
  production entry point bound to loopback, Caddy reverse-proxy deployment
  assets, integration adapter seams (Dagu, ReportPortal, KRE, runner,
  updater).
