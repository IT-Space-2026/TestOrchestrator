# Deployment — Waitress + Caddy

Production request flow:

```text
Browser ──HTTPS──▶ Caddy ──HTTP loopback──▶ Waitress 127.0.0.1:18080 ──WSGI──▶ Flask
```

- **Waitress** runs the Flask app as the production WSGI server, bound to
  `127.0.0.1:18080` — never a public interface (v1.8: internal app port is
  18080; public HTTP owns 8080). Containerized deployments must use a
  private service network without publishing the app port.
- **Caddy** (bundled pinned binary, `3rdparty/caddy/`) is the only public
  listener: HTTPS termination on the configured port (default 443), and an
  optional HTTP redirect-only listener (default 8080) that targets the
  configured HTTPS address — never the request Host, and never serves login
  or mutations. Its admin API is disabled (`admin off`), and
  `auto_https disable_redirects` guarantees only explicitly configured
  listeners are opened.
- The Flask development server/debugger is never used in production.

## Bootstrap (v1.8)

A fresh install provisions exactly one `Admin` account with the temporary
password `Admin` (Argon2id verifier only, `must_change_password=True`).

- Until the password is replaced **and** external access is explicitly
  enabled, the application refuses non-loopback clients at request level —
  independent of listener binding, Host headers, or client-supplied
  forwarded IPs. Keep Waitress loopback-bound during bootstrap (the
  default). Headless deployments: perform the password change on the local
  console or over an approved SSH tunnel; never expose `Admin`/`Admin` on
  the LAN.
- Installer option: set `TO_ADMIN_PASSWORD` (≥12 chars) in the environment
  before first start to replace the bootstrap credential outright; setup
  then completes without the shared temporary password.
- Login attempts are rate-limited; the session is rotated and
  `setup_completed` persisted after the change. Upgrades, restarts, and
  restores never recreate or reset the bootstrap credential.

## Forwarded headers

`TrustedProxyFix` (testorchestrator/security.py) applies `X-Forwarded-*`
handling **only** when the immediate peer is a configured trusted proxy
(default `127.0.0.1`, `::1` — the local Caddy). Forwarding headers from any
other peer are stripped, so client-supplied forwarding headers are never
trusted and cannot bypass the loopback gate.

## Certificates / protocol

Admin -> System -> Network & certificates supports:

- **Generate locally** — a unique per-installation CA signs a leaf for the
  selected DNS/IP SANs (validity ≤ 825 days). Browsers must trust the local
  CA: export `local-ca.pem` (public only) and install it into client trust
  stores with explicit consent. The private key is never offered as a
  download.
- **Import existing** — PEM certificate/full chain + matching private key,
  optional separate intermediates. Validated for format, key↔cert match,
  SAN coverage of the advertised host, current validity, and chain
  completeness. PKCS#12/PFX is not supported. Trust on any given client
  cannot be guaranteed — importing does not change CA trust on devices.
- **Storage** — keys live under `TO_CERT_DIR` (default `instance/certs`,
  deployed under `/var/lib/testorchestrator/certs`) with 0700/0600
  permissions, outside the repo, images, and Git exports; preserved through
  backup/restore/rotation. Operator keys are never baked into installers.
- **Config apply** — changes render a Caddyfile via
  `testorchestrator/caddyconf.py` (`tls <fullchain> <key>` for managed
  modes, `tls internal` fallback), validate with the bundled `caddy
  validate`, install only on success, and snapshot `previous_good_config`
  for one-click/CLI rollback. No arbitrary Caddyfile injection; changing
  the advertised address invalidates SAN coverage — re-validate and issue a
  new certificate rather than silently accepting.

## Post-quantum transport compatibility

Selecting Caddy and enabling HTTPS does **not** by itself satisfy the PQ
transport checks from the master specification, and certificate import does
not establish quantum-safe authentication. Before release sign-off, validate
the negotiated connection (TLS stack of the bundled Caddy 2.9.1 build
against supported clients) and document conventional certificate/signature
limitations.

## Layout on target host

| Path | Contents | Lifecycle |
|---|---|---|
| `/opt/testorchestrator` | payload: code, venv, `3rdparty/caddy/caddy`, `release/release.json` | replaced on upgrade/rollback |
| `/etc/testorchestrator` | Caddyfile (rendered), `web.env` (`TO_SECRET_KEY`, `TO_DATABASE_URI`, `TO_ADMIN_PASSWORD` at install only, `TO_ALLURE_JAVA_HOME`/`TO_ALLURE_JAVA_RECORD` when Allure Java validated), `allure-java.json` | persistent |
| `/var/lib/testorchestrator` | SQLite DB (users incl. bootstrap state, settings), `certs/` (keys 0600), instance data, logs | persistent |

Persistent configuration, certificates, and application data stay outside
the replaceable binaries so upgrades never overwrite them. Each payload
carries its own `release/release.json`, so About always shows the
version/build of what is installed (AB04), including after rollback.

## Services

`deploy/testorchestrator-web.service` (Waitress on 127.0.0.1:18080) and
`deploy/testorchestrator-caddy.service` (bundled Caddy) are reference
systemd units covering startup/shutdown/restart; the installer wires health
checks (`flask health`, `caddy validate`), logs, upgrades, and recovery.
Users never install Python packages or configure a web server manually.
The installer does not change OS firewall rules without explicit approval;
rules are scoped to selected profiles/interfaces.

## Build stamping

`python3 scripts/stamp_release.py` rewrites `release/release.json` with the
packaged commit as the build identifier at release time.
