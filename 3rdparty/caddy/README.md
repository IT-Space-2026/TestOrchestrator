# Caddy — bundled third-party web server / reverse proxy

Caddy is the public-facing web server for TestOrchestrator deployments. It
terminates HTTPS (organization-issued or unique locally generated
certificates), performs HTTP-to-HTTPS redirects, and reverse-proxies to
Waitress on loopback (`127.0.0.1:8080`). Only Caddy's configured HTTP/HTTPS
ports are exposed; its admin API must not be published.

## Pinned upstream metadata

| Field | Value |
|---|---|
| Component | Caddy |
| Version | 2.9.1 (`v2.9.1`) |
| Upstream | https://github.com/caddyserver/caddy |
| Source archive | `caddy_2.9.1_linux_amd64.tar.gz` |
| Platform | linux/amd64 |
| SHA-256 (extracted `caddy` binary) | see `caddy.sha256` |
| Licence | Apache License 2.0 — see `LICENSE` |

`caddy` here is the extracted upstream release binary. Re-fetch reproducibly
with `scripts/fetch_caddy.sh`, which verifies the recorded digest.

## Configuration

- Template Caddyfile: `deploy/Caddyfile` (copied to the config area at
  install time — persistent config lives outside replaceable binaries).
- The bundled build's negotiated TLS stack must be validated against the
  post-quantum transport compatibility checks from the master specification;
  enabling HTTPS alone does not satisfy them. Conventional certificate/
  signature limitations are documented in `docs/DEPLOYMENT.md`.
