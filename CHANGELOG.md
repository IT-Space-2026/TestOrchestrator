# TestOrchestrator changelog

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
