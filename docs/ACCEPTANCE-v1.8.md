# v1.8 acceptance mapping — Bootstrap Admin, access URLs, certificate import

Spec: `testorchestrator-specification-extension-v1.8.md` (normative
extension; merged into canonical requirements).

## Bootstrap (§2)

| ID | Requirement | Implementation | Verified by |
|---|---|---|---|
| BOOT01 | Admin/Admin only via restricted local setup | `bootstrap.ensure_bootstrap_admin()` provisions exactly one `Admin`/`Admin` (Argon2id, `must_change_password=True`) when the user table is empty; gate refuses non-loopback peers until `setup_completed AND external_enabled`; `TO_ADMIN_PASSWORD` installer path replaces the bootstrap credential before serving | `test_boot01_*` |
| BOOT02 | Mandatory strong change, no API/URL bypass | Gate allowlists only `auth.change_password`, `auth.logout`, `static` while the flag is set; strong-password validator (≥12, upper+lower+digit, ≠ `Admin`, ≠ current); login rate limiter; session rotated on success; `setup_completed` persisted in DB | `test_boot02_*` |
| BOOT03 | Remote blocked until changed + explicitly enabled; header spoofing fails | Gate checks the post-ProxyFix peer address; `TrustedProxyFix` strips forwarding headers from untrusted peers, so a spoofed `X-Forwarded-For` cannot produce a loopback `remote_addr` | `test_boot03_*` |
| BOOT04 | Upgrade/restart/restore never resets Admin/Admin | Provisioning is a no-op once any user exists; account state lives in the persistent DB, not the payload | `test_boot04_*` |

## Address model & ports (§3–§4, §6)

| ID | Requirement | Implementation | Verified by |
|---|---|---|---|
| NET01 | HTTPS 443, public HTTP 8080, Waitress 18080 — no collision | `settings.DEFAULTS`; `Config.WAITRESS_LISTEN=127.0.0.1:18080`; `check_port_conflicts` on save | `test_net01_default_ports` |
| NET02 | Links correct for hostname/IPv4/IPv6/nondefault ports | `network.build_url`/`access_links` bracket IPv6, omit :443 only | `test_net02_*` |
| NET03 | Multi-interface suggestion requires choice; no public-IP claims | `detect_address_candidates()` is local-only (UDP routing lookup + getaddrinfo, no external service), marks `is_private`; UI lists candidates with a required radio/custom field | `test_net03_*`, network page |
| NET04 | HTTP redirect-only to configured address; never login over HTTP | Rendered Caddyfile emits `redir https://<configured>{uri}` — `{host}` never appears; `auto_https disable_redirects` prevents implicit listeners | `test_net04_*` |
| NET05 | Invalid/conflicting ports & uncovered SANs rejected; failed apply keeps prior config | `validate_port`/`check_port_conflicts`; SAN coverage enforced in `certs.import_pem`; staged apply writes active config only after `caddy validate` succeeds; `previous_good_config` + rollback | `test_net05_*`, `test_cert01_san_mismatch_rejected` |

## Certificates (§5)

| ID | Requirement | Implementation | Verified by |
|---|---|---|---|
| CERT01 | Valid PEM chain/key import works; bad pairs blocked | `certs.import_pem`: PEM parse, key↔cert public-key match, validity window, SAN coverage of advertised host, chain completeness to a self-issued top; intermediates file supported; PKCS#12 explicitly not supported | `test_cert01_*` |
| CERT02 | Generated cert unique per install; trust instructions accurate | `certs.generate_local`: per-install CA (3072-bit) + leaf (2048-bit, ≤825 days) for selected SANs; UI explains client trust-store requirement; CA cert export allowed, key never | `test_cert02_*` |
| CERT03 | Key absent from audit/log/Git/image/UI; restricted on disk | Keys written via `_write_protected` (dir 0700, keys 0600) under `TO_CERT_DIR` (default `instance/certs`, gitignored); audit lines carry fingerprints only; `/system/network/ca.pem` serves only the public CA | `test_cert03_*` |
| CERT04 | DNS/firewall/NAT prerequisites separate from settings | Network page explains DNS resolution and NAT/firewall are external; no port-forwarding is attempted | `test_network_page_shows_prerequisites` |

## Notable scope notes

- Fresh-install remote blocking relies on `remote_addr` after proxy fixing;
  headless deployments should keep Waitress loopback-bound until setup is
  complete (documented in `docs/DEPLOYMENT.md`).
- HTTP `development-insecure` mode exists in the schema per policy but the
  renderer emits no HTTP listener for it — Caddy serves HTTPS-only; a real
  insecure mode would need separate approval work.
- Certificate import does not establish quantum-safe authentication; TLS
  group negotiation checks remain a pending v1.0–v1.6 validation item (see
  `docs/DEPLOYMENT.md`).
