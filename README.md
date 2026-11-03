# TestOrchestrator

Test orchestration web application for **IT-Space Kft.** — Flask + Jinja2
server-rendered UI, locally bundled CSS, minimal vanilla JavaScript. No
Node.js, npm, frontend build server, or SPA.

This repository currently implements the v1.0–v1.6 foundation (roles,
authentication, audit, System pages, deployment assets, integration seams)
plus **v1.7 About/IT-Space branding** and **v1.8 bootstrap Admin, access
URLs, and certificate setup**. See `docs/ACCEPTANCE-v1.7.md` and
`docs/ACCEPTANCE-v1.8.md` for the acceptance-criteria mappings.

## Architecture

```text
Browser
   │ HTTPS (443, configurable)
   ▼
Caddy (3rdparty/caddy/)          — TLS termination, HTTP→HTTPS redirect (8080)
   │ HTTP over loopback
   ▼
Waitress — 127.0.0.1:18080       — production WSGI server (never public)
   │ WSGI
   ▼
Flask / TestOrchestrator (testorchestrator/)
```

- `testorchestrator/` — application package (factory in `__init__.py`)
  - `auth/` — login/logout, forced + self-service password change
    (Flask-Login, Flask-WTF CSRF, Argon2id hashing)
  - `main/` — dashboard, About, third-party notices, admin System pages
  - `system/` — Admin -> System -> Network & certificates
  - `bootstrap.py` — Admin/Admin provisioning, loopback/host gates, rate
    limiting (v1.8)
  - `settings.py` — durable settings schema (v1.8 §6)
  - `network.py` — address model, candidates, URL builders, validation
  - `certs.py` — local CA/leaf generation and PEM import validation
    (`cryptography`)
  - `caddyconf.py` — Caddyfile renderer + staged apply/rollback
  - `adapters/` — seams for Dagu, ReportPortal, KRE, runner agent, updater.
    Long-running work runs outside request handlers; do not implement
    integrations inside Flask views.
  - `models.py` — `User` (roles: Admin, Test Manager, Tester, PO),
    `AuditEvent`, `Setting`
- `assets/branding/` — owner-approved branding assets (approval tracked in
  its README; the official logo is **not yet supplied**, see AB07)
- `3rdparty/` — bundled third-party notices/licences and the pinned Caddy
  binary (`3rdparty/caddy/`)
- `release/release.json` — bundled version/build metadata shown in About
- `deploy/` — Caddyfile template and systemd units
- `scripts/` — `stamp_release.py` (build stamping), `fetch_caddy.sh`
  (reproducible, checksum-verified Caddy fetch)
- `tests/` — pytest suite covering AB01–AB07 and security basics

## Development

```bash
python3 -m virtualenv .venv          # or: python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

export FLASK_APP=wsgi:app
export TO_SECURE_COOKIES=0           # local HTTP dev only
.venv/bin/flask db upgrade
.venv/bin/flask seed-users --dev-defaults   # seeds manager/tester/po only
.venv/bin/waitress-serve --listen=127.0.0.1:18080 wsgi:app
```

On a fresh database the first request provisions the bootstrap account
`Admin`/`Admin`; signing in forces a strong password change before anything
else is reachable (v1.8). To skip the shared bootstrap credential during a
headless install, set `TO_ADMIN_PASSWORD` (≥12 chars) before first start.

Run tests: `.venv/bin/python -m pytest tests/ -q`

## Production deployment

See `docs/DEPLOYMENT.md`. Summary: Waitress binds `127.0.0.1:8080` (not a
public interface), Caddy is the only public listener, forwarding headers are
trusted only from the loopback proxy, and persistent config/data lives in
`/etc/testorchestrator` + `/var/lib/testorchestrator`, separate from the
replaceable payload in `/opt/testorchestrator`.

## Known incomplete requirement (AB07)

The official IT-Space Kft. logo has not been supplied/approved. A clearly
labelled temporary placeholder ships in its place; production branding
acceptance is blocked until the approved asset lands in `assets/branding/`
and replaces `LOGO_APPROVED = False` in
`testorchestrator/main/routes.py`. Do not invent, redraw, or download a logo.
