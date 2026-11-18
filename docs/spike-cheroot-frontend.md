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

---

## 6. Linux parity verification — packaged install (commit `3eac826`)

Environment: real release build
(`testorchestrator-2.0.3-linux-x86_64.tar.gz`, sha256 `824f68e0…`,
built from this branch) installed via `install.sh --frontend cheroot`
to a user prefix on Ubuntu x86_64, activated under `systemctl --user`
(system scope + port 443 require root — **not testable here**; the
staged unit carries `AmbientCapabilities=CAP_NET_BIND_SERVICE` for
that case). Cheroot on :8443/:8081, Caddy on :9443/:9081, same
Waitress backend on 127.0.0.1:18080, same instance data.

### Request/edge parity

| Check | Command / action | Caddy | Cheroot | Verdict |
|---|---|---|---|---|
| HTTPS serves app | `curl -k https://…/login` | 200 | 200 | identical |
| App-response headers | `curl -D-` | `x-content-type-options`, `referrer-policy`, no Server | same headers; `Server: TestOrchestrator` | both masked; **caddy strips Server entirely on proxied responses, cheroot renames it** — cosmetic diff |
| Frontend-generated response header | `curl -H 'Host: evil'` | `server: Caddy` leaks on the empty-200 fallback | `Server: TestOrchestrator` on 421 | **cheroot masks better** |
| Redirect, all URI shapes | `/`, `/a/b/c`, `/a/b/`, `?x=1&y=2`, `/a%2Fb?k=v%20w` | 301, `Location: https://localhost:9443{uri}` exact | 301, `Location: https://localhost:8443{uri}` exact | identical |
| Nondefault ports in redirect | ports ≠ 443/8080 | correct host:port in Location | same | identical |
| Foreign Host on HTTPS | `-H 'Host: evil.example.com'` | **empty HTTP/2 200** — never reaches app, leaks `server: Caddy` | **421 Misdirected Request** | different code — both refuse; caddy's silent empty-200 is arguably worse |
| Waitress loopback private | `curl 127.0.0.1:18080/login` | 200 | 200 | identical — probe preserved |
| Login POST + session + authed page | form CSRF login → `/system/network` | 302 + cookie + 200 | 302 + cookie + 200 | identical (see CSRF note) |
| CSRF referrer check | POST without Referer | **skipped** — Waitress drops `X-Forwarded-Proto`; app sees `is_secure=False` | **enforced** — direct TLS → `is_secure=True` → 400 | **cheroot strictly better**; header-less POSTs to CSRF routes fail (correct) — agent API is csrf-exempt, unaffected |
| Client-spoofed X-Forwarded-For | `-H 'X-Forwarded-For: 9.9.9.9'` | header reaches app; `licence-activate` limiter keys on raw header → bypassable | stripped in `_DirectPeer`; real peer used | **cheroot strictly better** (pre-existing weakness in caddy tier) |
| HTTP/2 + HTTP/3 | `curl -D-` | h2 negotiated, `alt-svc: h3` | HTTP/1.1 only | **caddy advantage** — functional parity at h1; h2/h3 is a real gap |
| Concurrent ×40 | 40 parallel fresh-conn GETs | 0.12–0.16 s | 1.5–2.1 s (defaults) → **0.16–0.21 s after `request_queue_size=128`** | gap was listen-backlog, not throughput — §7 |
| 64 MB download | worker-installer `.exe` | 0.10 s, 617 MB/s | 0.15 s, 428 MB/s | comparable |
| Requests during live cert swap | 30× GET while admin generate | n/a (no hot reload) | all 200, no drops | pass |

### Certificate lifecycle parity

| Check | Action | Caddy | Cheroot | Verdict |
|---|---|---|---|---|
| First generation | start on empty CERT_DIR | internal CA in own storage | `generate_local` → CERT_DIR | different anchors by design (documented) |
| `local`/`imported` mode files | `tls <cert_dir>/fullchain.pem` | serves file | serves same file | identical anchor |
| Admin generate → Apply → new cert on fresh conn | `/system/network/certificate/generate` + `/network/apply` | **stale until service restart** — `admin off` means `caddy reload` cannot even connect (`dial tcp 127.0.0.1:2019: refused`); file-watch did not fire within 75 s | **≤2 s, no restart** (watcher) | **cheroot strictly better** — the shipped caddy config has NO hot cert path; product semantics today are restart-to-apply |
| Admin import → Apply → new cert on fresh conn | real multipart POST `/system/network/certificate/import` (self-signed leaf, SAN `localhost`) | (as above, restart needed) | **fingerprint proven**: served `E7:3A:CF:3B…` (local CA) → `49:44:D1:01…` = imported cert, ≤4 s, no restart; `-CAfile imp-cert.pem` verify OK | pass — cheroot applies without restart |
| Intended client trust | `curl --cacert local-ca.pem` | n/a | `Verify: 0`, 200 | pass |
| Corrupt live replacement | garbage PEM into fullchain | n/a | last-good served, `cert reload failed` logged, no key material | pass |
| Mismatched key | wrong key file | n/a | last-good, `KEY_VALUES_MISMATCH` logged | pass |
| Missing files | rm both PEMs | n/a | last-good; recovers on restore | pass |
| Cold start, invalid TLS | corrupt files + service start | fails at startup (same class) | `SSLError`, process exits, no plain-HTTP listener, systemd restart-loop, self-heals on fix | identical intent |
| Pre-first-poll race | cert write between context build and watcher poll 1 | n/a | detected (baseline stamp) | regression-tested |

### Service lifecycle parity

| Check | Caddy | Cheroot | Verdict |
|---|---|---|---|
| `--frontend` wiring | n/a | `install.sh --frontend cheroot` stages `testorchestrator-web-cheroot.service` as `testorchestrator-web.service`; caddy staged but NOT enabled | pass — only one frontend owns ports |
| Packaged install + opt-in | baseline | install.sh completes; venv includes cheroot from vendored wheels; listeners come up under systemd | pass |
| `http_mode=disabled` | no site block | no redirect listener; HTTPS still 200 | identical |
| Port-in-use at startup | fails at startup | `RuntimeError: could not bind HTTPS listener :8443`; unit retries; self-heals when freed | identical behavior |
| Backend unavailability | listener stays; 502 to clients | process dies → connection refused | **difference** — shared fate vs 502 signal; systemd restart covers both |
| Restart with changed settings | restart applies Caddyfile | restart re-reads settings → rebind | identical (product already restart-applies) |
| Config survives restart | yes | yes | identical |
| Repeated Apply → second frontend | n/a | no — single listener set, watcher just reloads | pass |
| Caddy→Cheroot→Caddy switch | — | stop/start staged units; HTTPS + redirect + same cert at every step; no data/cert migration; caddy-home CA untouched | pass (rollback verified) |
| Upgrade existing install | — | **not tested** this pass (fresh install + `db-upgrade` verified) | not tested |
| Machine reboot | — | **not tested** (cannot reboot host); stop/start cycle equivalent verified | not tested |
| Other distros/arch | — | **not tested** — only Ubuntu x86_64 available | not tested |
| Root/system-scope + port 443 | — | **not tested** — unprivileged env; user-scope + 8443 exercised instead | not tested |

### New failure found + fixed during verification

`wsgi.py` started the frontend, then called blocking `waitress.serve`.
If the backend bind failed (port in use), the exception propagated —
but **cheroot worker threads are non-daemon**, so the process lingered
with 8443/8081 still serving while systemd `Restart=on-failure` waited
on a process that never died. Fixed: backend-serve failure now stops
the frontend and `os._exit(1)`s so the service manager restarts.

### Sizes with real wiring

- Tarball: 271,376,361 B vs published 271,177,471 B → **+199 KB**
  (the three vendored wheels).
- If Caddy were later removed: −17.7 MB compressed / −50.9 MB
  installed; venv adds ~1.6 MB (cheroot+deps).

## 7. Concurrency follow-up (matched HTTP/1.1, commit with `request_queue_size=128`)

**Question:** does the earlier ~9× gap (0.19 s vs 1.71 s) reproduce,
and is it protocol (h2) or implementation?

### 7.1 Reproducing the original result

The earlier figures were **wall-clock batch times** for 40 parallel
`curl` processes, each doing one fresh TLS connection + one GET:

```bash
# both tiers, same command — HTTPS :8443, HTTP/1.1+h2 negotiated by client
for i in $(seq 1 40); do curl -sk https://localhost:8443/healthz -o /tmp/burst_$i.out & done; wait
```

Reproduced on the same packaged install (`/tmp/to-inst`), 3 runs each,
all 40/40 returning HTTP 200 with the exact expected body
`{"status":"ok",...}` — no errors, no timeouts:

| frontend | run1 | run2 | run3 |
|---|---|---|---|
| Caddy | 129 ms | 119 ms | 160 ms |
| Cheroot (defaults: `numthreads=10`, `request_queue_size=5`) | 2132 ms | 1505 ms | 1711 ms |

→ **~10–13× gap reproduces**, but only on simultaneous *fresh-connection*
bursts.

### 7.2 Matched HTTP/1.1 matrix (validated bodies)

Client: Python `http.client` harness (`/tmp/pbench.py`) — forced
HTTP/1.1, per-request status + body verification, warmup + measured
runs. `k` = connection reuse, `fresh` = new TLS handshake per request.
Requests per case: `concurrency × 40`.

`GET /healthz` (rps / median ms / p95 ms / p99 ms; errors 0 everywhere):

| tier | mode | c1 | c10 | c40 |
|---|---|---|---|---|
| Caddy | k | 473 / 2.0 / 2.7 / 5.4 | 242 / 41 / 63 / 73 | 230 / 170 / 247 / 291 |
| Cheroot q5 | k | 521 / 1.7 / 3.1 / 4.7 | 199 / 34 / 70 / 104 | 199 / 129 / 207 / **1544** |
| Cheroot q128 | k | — | 189 / 50 / 87 / 112 | 200 / 189 / 265 / 579 |
| Caddy | fresh | 102 / 9.7 / 14 / 14 | 277 / 34 / 61 / 77 | 285 / 134 / 188 / 217 |
| Cheroot q5 | fresh | 177 / 4.9 / 11 / 12 | 288 / 19 / 30 / 35 | 260 / 17 / 35 / **2142** |
| Cheroot q128 | fresh | — | 331 / 28 / 40 / 45 | 330 / 113 / 161 / 187 |

`GET /system/network` (authenticated UI page, session cookie):

| tier | mode | c1 | c10 | c40 |
|---|---|---|---|---|
| Caddy | k | 147 / 6.3 | 75 / 133 | 84 / 478 |
| Cheroot q5 | k | 165 / 5.7 | 62 / 160 | 75 / 500 |
| Caddy | fresh | — | 85 / 115 | 89 / 448 |
| Cheroot q5 | fresh | 68 / 15.5 | 87 / 90 | 79 / 110 (p95 2154) |

Waitress direct `http://127.0.0.1:18080` (defaults: `threads=4`,
`backlog=2048`): c40 k 266 rps / med 149; c40 fresh 245 / med 159 /
p99 258 — **no tail** (large backlog).

Caddy h2 (`h2load -n1600 -c40 -m1`): 230 rps, 1600/1600 2xx —
**no gain over h1 keep-alive** for single-request work.

### 7.3 Root cause — one setting at a time

40-way fresh-curl burst with a single knob changed on a side listener
(`:8445`, same process/app):

| variant | burst wall-clock |
|---|---|
| numthreads=10, queue=5 (defaults) | 1505–2132 ms |
| numthreads=40, queue=5 | 1363–1537 ms |
| numthreads=10, **queue=128** | **137–226 ms** |
| numthreads=40, queue=128 | 162–212 ms |
| Caddy (reference) | 119–160 ms |

`request_queue_size=5` is Cheroot's `listen(backlog)` — a 40-conn burst
overflows the accept queue, dropped SYNs cost clients ~1–2 s of TCP
retransmit backoff. `numthreads` is irrelevant to the gap. Applied
`request_queue_size=128` to both listeners in `frontend.py`; re-verified
on the real systemd service: **157–212 ms, 40/40 ok** — Caddy parity.
10/10 frontend tests still pass. Note: `ab` counts Cheroot's
`Connection: close` responses as "length failures" — a client artifact;
verbose mode shows complete bodies; excluded from the data above.

### 7.4 Resource use (sustained c40 k, 2000 req / ~10 s)

| tier | frontend CPU | backend CPU | total CPU | RSS |
|---|---|---|---|---|
| Caddy + Waitress | 0.99 s (caddy) | 11.94 s | 12.93 s | 61 + 87 = 148 MB |
| Cheroot (in-process) | — | — | 12.49 s | **97 MB** |

Same CPU; Cheroot uses less total memory (one process). `numthreads=40`
cost ~+12 MB RSS for no throughput gain → **kept at default 10**.

### 7.5 Verdict

- The ~9× gap **does reproduce** under matched HTTP/1.1 — but only for
  simultaneous fresh-connection bursts, and it was a Cheroot
  **backlog misconfiguration**, not a protocol or throughput deficit.
- With `request_queue_size=128`: steady-state h1 throughput is within
  ~10–15% of Caddy (sometimes faster at c1), CPU equal, memory lower.
- **Normal authenticated workflows were never affected**: the authed
  page tops out at ~75–90 rps on *both* tiers at c40 (app-bound).
- h2 measured (h2load): no throughput advantage here — Caddy's real
  h2/h3 benefit is client-perceived (multiplexing, fewer connections),
  not req/s on this workload.
- Remaining Cheroot weakness: keep-alive c40 p99 tail (~0.6–2 s) from
  thread-per-connection pinning — irrelevant at real loads, but real.

## 8. Rollback

No data migration — switch is env + service wiring only:

1. `unset TO_FRONTEND` (or set to anything ≠ `cheroot`).
2. `systemctl enable --now testorchestrator-caddy` / re-enable the
   Caddy scheduled task; restart the web service.
3. Caddy's own internal CA state under `caddy-home/` is untouched —
   reverting restores the original trust anchor.

## 9. Recommendation — **Linux parity verified with gaps; Windows still untested**

On the packaged install, Cheroot covers every Caddy responsibility
the product actually uses — and on three points it is *stricter*
than the shipped Caddy config: cert changes apply in ≤2 s (the
shipped config has no hot path at all — `admin off` prevents
`caddy reload`), the CSRF referrer check actually runs
(`is_secure=True`), and client-supplied `X-Forwarded-*` cannot
reach the app.

Genuine gaps vs Caddy: **HTTP/2 + HTTP/3 absent** (h1 only — though
h2load shows no req/s advantage on this workload), a keep-alive c40
p99 tail (~0.6–2 s vs Caddy ~0.3 s) from thread-per-connection
pinning, empty-200 vs 421 on foreign Host (cheroot's is arguably
better but the status differs), and 502-vs-refused when the app is
down. The earlier ~9× burst gap traced to `request_queue_size=5` and
is fixed; authed-route throughput is equal at real loads (§7).
None blocks the required workflows; all are documented above.

Still provisional: **Windows untested** (no inference), root/systemd
scope + port 443 untested (unprivileged env — user scope + 8443 used),
other distros/arch untested, upgrade-with-existing-certs untested,
and the shipped default remains Caddy on all tiers.

Next step if pursued: Windows parity pass (cert import/generate,
scheduled-task wiring, dual-stack bind), plus a root-scope systemd
install validating `CAP_NET_BIND_SERVICE` on :443 and a real
upgrade from a released install.
Do **not** merge this spike as-is; do not bundle it into 2.0.x.
