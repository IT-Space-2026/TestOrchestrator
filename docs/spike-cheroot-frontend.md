# Spike: replacing Caddy with an in-process frontend (Cheroot)

Branch: `spike/cheroot-frontend` (commit `53e0c40`). Opt-in only —
Caddy remains the default frontend on every tier.

## 1. What Caddy actually does today (verified)

Source: `deploy/Caddyfile`, `testorchestrator/caddyconf.py`,
`deploy/testorchestrator-caddy.service`, `scripts/install.py`,
`packaging/windows/app-installer.nsi`, `testorchestrator/system/routes.py`,
`testorchestrator/certs.py`.

| Behavior | Where | Verdict |
|---|---|---|
| TLS termination, public HTTPS :443 | `tls {$TO_TLS_MODE:internal}` / `tls <fullchain> <key>` | required |
| Local CA (`internal` mode) | Caddy's own CA **or** `certs.generate_local` (Python) | required — Python impl already exists |
| Imported org certs | `tls <fullchain> <key>` under protected `CERT_DIR` | required |
| Reverse proxy → Waitress `127.0.0.1:18080` | `reverse_proxy` | required (or equivalent direct serve) |
| HTTP→HTTPS redirect :8080, configured target, `{uri}` preserved | `redir … permanent` | required |
| Host allowlist = configured site labels only | site block labels | required |
| `Server` header masked; `X-Content-Type-Options`, `Referrer-Policy` | `header { -Server … }` | required — **Flask already emits both**; `-Server` parity needed |
| Caddy admin API | `admin off` | disabled — unused |
| Automatic listeners / ACME redirects | `auto_https disable_redirects` | unused |
| Static files, SPA fallback, compression, WebSockets, request limits, custom timeouts | — | **not used** |

**No ACME anywhere** — no Let's Encrypt, no Lego, no `acme` package needed.
Cert lifecycle is: `certs.generate_local` (managed CA+leaf) or admin-imported
files, then `caddy reload` via staged apply. A replacement needs TLS
termination + file-based certs + hot reload — nothing else.

## 2. Baseline measurements

| | Caddy | Cheroot 11.1.2 + deps |
|---|---|---|
| Version | bundled binary reports **v2.11.9** (README still says 2.9.1 — metadata drift) | 11.1.2 pinned |
| Raw payload, Linux | `3rdparty/caddy/caddy` = **50,892,960 B** | wheels ≈ **215 KB** |
| Compressed contribution | ≈ **17.7 MB** (gzip-9) / ~18 MB (zip, Windows) | ≈ 0.2 MB |
| Installed footprint | ~48.5 MiB + Caddy state dir | cheroot 936 KB + jaraco.functools 104 KB + more_itertools 604 KB ≈ **1.6 MB** |
| Extra runtime deps | none (static Go binary) | pure Python, already-bundled interpreter |

Package totals today: Linux tar.gz 271.2 MB → ~253.5 MB (−6.5%);
Windows installer 306.9 MB → ~289 MB (−5.8%). Same build settings.

## 3. Options considered

| Option | Verdict |
|---|---|
| **Waitress alone** | Cannot terminate TLS (no TLS support by design — waitress docs require a TLS-terminating proxy). Would need cert handling anyway → rejected. |
| **Cheroot (chosen)** | Mature pure-Python WSGI server (CherryPy lineage) with native `BuiltinSSLAdapter`, per-connection context lookup → hot cert reload without threads restart. BSD-3-Clause. |
| Cheroot + Lego / + `acme` | **Unnecessary** — no ACME in use; adding it would expand scope beyond verified requirements. |
| Keep Caddy | Zero effort; costs ~18 MB compressed, ~49 MB installed, a second process + service unit/scheduled task. |

Redistribution: Cheroot BSD-3-Clause, jaraco.functools MIT,
more_itertools MIT — all commercially redistributable, notice-only.
Caddy is Apache-2.0 (no redistribution problem either way; this is
size/complexity, not licence, driven).

## 4. Prototype (`TO_FRONTEND=cheroot`)

`testorchestrator/frontend.py` — started from `wsgi.py` __main__ only:

- Cheroot HTTPS listener on `https_port`, dual-stack `::` → `0.0.0.0`
  fallback; serves the same Flask app directly (proxy hop removed;
  Waitress keeps `127.0.0.1:18080` for internal probes).
- Certs: `ensure_certificates` uses managed `CERT_DIR/fullchain.pem` +
  `private-key.pem`; on first start with `internal` mode it calls
  `certs.generate_local` — the same Python CA used for imported/local
  modes. **Trust anchor changes on switch** (Caddy's CA ↔ Python CA).
- HTTP redirect listener when `http_mode == "redirect"`: 301 to the
  configured HTTPS address, `{uri}` preserved, inbound Host never
  reflected — parity with the Caddyfile.
- `_HostGate`: 421 for Host values outside configured site labels.
- `_DirectPeer`: strips client-supplied `X-Forwarded-*` (no trusted
  proxy in this tier) and normalises `::ffff:` mapped peers.
- `server_name="TestOrchestrator"` masks the `Server` header.
- **Hot cert reload**: watcher thread compares file mtimes against the
  stamp captured when the live context was built, swaps
  `adapter.context` on change — admin regenerate/import takes effect
  on new connections, no restart, last-good context kept on failure.

## 5. Test results — `tests/test_frontend_cheroot.py` (7 tests)

```
python -m pytest tests/test_frontend_cheroot.py -xvs
# 7 passed in 10.73s
```

| Check | Linux | Windows |
|---|---|---|
| HTTPS serves app (200, login page) | pass | **not tested** |
| HTTP→HTTPS redirect, URI preserved | pass | not tested |
| `http_mode=disabled` → no redirect listener | pass | not tested |
| Host allowlist → 421 for foreign Host | pass | not tested |
| Managed certs generated + served | pass | not tested |
| Hot cert reload (fp changes, no restart) | pass | not tested |
| `Server` header masked | pass | not tested |

Renewal-failure behavior: a failed cert build keeps the previous
context (logged) — last valid cert stays usable. No issuance/renewal
CA path exists to test (no ACME). Startup/shutdown/restart = process
lifecycle; `Frontend.stop()` stops listeners and the watcher.

## 6. Rollback

No data migration — switch is env + service wiring only:

1. `unset TO_FRONTEND` (or set to anything ≠ `cheroot`).
2. `systemctl enable --now testorchestrator-caddy` / re-enable the
   Caddy scheduled task; restart the web service.
3. Caddy's own internal CA state under `caddy-home/` is untouched —
   reverting restores the original trust anchor.

## 7. Recommendation — **provisional follow-up, not merge**

The prototype meets every verified Caddy behavior on Linux with
~17.7 MB compressed / ~49 MB installed savings per platform and no
new obligations. **But**: Windows is untested (no inference from
Linux), the installer/service wiring (systemd unit selection, NSIS
scheduled tasks, `caddyconf.py` render+validate path, admin "apply"
reload trigger) is deliberately untouched, and `ensure_certificates`
trust-anchor switch needs a documented client-trust step on cutover.

Recommended next step: a follow-up experiment on Windows only —
cert generation/import parity, scheduled-task wiring, dual-stack
bind behavior — plus packaging the service-selection switch.
Do **not** merge this spike as-is; do not bundle it into 2.0.x.
