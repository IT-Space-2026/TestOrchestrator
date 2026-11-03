# TestOrchestrator changelog

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
