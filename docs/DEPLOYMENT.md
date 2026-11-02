# Deployment — Waitress + Caddy

Production request flow:

```text
Browser ──HTTPS──▶ Caddy ──HTTP loopback──▶ Waitress 127.0.0.1:8080 ──WSGI──▶ Flask
```

- **Waitress** runs the Flask app as the production WSGI server, bound to
  `127.0.0.1:8080` — never a public interface. (Containerized deployments
  must use a private service network without publishing the app port.)
- **Caddy** (bundled pinned binary, `3rdparty/caddy/`) is the only public
  listener: HTTPS termination, HTTP→HTTPS redirect. Its admin API is
  disabled (`admin off` in `deploy/Caddyfile`).
- The Flask development server/debugger is never used in production.

## Forwarded headers

`TrustedProxyFix` (testorchestrator/security.py) applies `X-Forwarded-*`
handling **only** when the immediate peer is a configured trusted proxy
(default `127.0.0.1`, `::1` — the local Caddy). Forwarding headers from any
other peer are stripped, so client-supplied forwarding headers are never
trusted.

## Certificates / protocol

- HTTPS enabled by default. `deploy/Caddyfile` supports:
  - organization-issued certificates (`tls /path/cert.pem /path/key.pem`), or
  - unique locally generated certificates (`tls internal`) — requires the
    documented client trust setup for the local CA.
- HTTP listener is redirect-only. Plain-HTTP application mode requires an
  explicit approved development configuration.
- Private keys live under the protected config area (`/etc/testorchestrator`
  or the Caddy data dir), not in the application payload.
- Hostname, bind addresses, and HTTP/HTTPS ports are configurable via
  `TO_HOSTNAME` / Caddyfile environment placeholders.
- Certificate validation, expiry warnings, rotation, safe config testing
  (`caddy validate`), rollback, and local recovery are installer concerns.

## Post-quantum transport compatibility

Selecting Caddy and enabling HTTPS does **not** by itself satisfy the PQ
transport checks from the master specification. Before release sign-off,
validate the actually negotiated connection (TLS stack of the bundled Caddy
2.9.1 build against supported clients) and document conventional
certificate/signature limitations. Pending — tracked with the v1.0–v1.6
consolidated requirements.

## Layout on target host

| Path | Contents | Lifecycle |
|---|---|---|
| `/opt/testorchestrator` | payload: code, venv, `3rdparty/caddy/caddy`, `release/release.json` | replaced on upgrade/rollback |
| `/etc/testorchestrator` | Caddyfile, `web.env` (`TO_SECRET_KEY`, `TO_DATABASE_URI`, …), certs/keys | persistent |
| `/var/lib/testorchestrator` | SQLite DB, instance data, logs | persistent |

Persistent configuration, certificates, and application data stay outside
the replaceable binaries so upgrades never overwrite them. Each payload
carries its own `release/release.json`, so About always shows the version/
build of what is installed (AB04), including after rollback.

## Services

`deploy/testorchestrator-web.service` (Waitress) and
`deploy/testorchestrator-caddy.service` (bundled Caddy) are reference
systemd units covering startup/shutdown/restart; the installer wires health
checks (`flask health`, `caddy validate`), logs, upgrades, and recovery.
Users never install Python packages or configure a web server manually.

## Build stamping

`python3 scripts/stamp_release.py` rewrites `release/release.json` with the
packaged commit as the build identifier at release time.
