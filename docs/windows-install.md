# TestOrchestrator — Windows installation & client access

Applies to `testorchestrator-<ver>-windows-amd64-setup-UNSIGNED.exe`
(the full central-server installer: app + embedded Python + Node +
Caddy + Dagu + Allure 3). The KRE execution engine is licensed and
installed separately by the customer — never bundled.

## Install & first access (default local mode)

1. Run the installer **elevated** (admin) — required for `%ProgramFiles%`
   payload, `%ProgramData%` state, Scheduled Tasks, and machine trust.
2. Unattended: `setup.exe /S [/D=<path>] [/PORT=8080] [/ADDR=localhost]`.
3. The installer registers and **immediately starts** four
   SYSTEM/ONSTART Scheduled Tasks — `TestOrchestratorWeb` (Waitress,
   `127.0.0.1:18080`), `…Worker`, `…Dagu` (`:29877`), `…Caddy`
   (HTTPS `:443`). **No reboot is required**; tasks also restart at boot.
4. Browse to `https://localhost/` → licence page → enter the
   manually supplied licence key → login → use.

## Certificate trust (what the installer does for you)

- Mode `tls internal`: Caddy generates a **fresh, per-installation local
  CA** at first start under
  `%ProgramData%\TestOrchestrator\caddy-home\Caddy\pki`. The private key
  is created on the machine, never shipped in the package and never
  shared between installations.
- The installer waits for `…\authorities\local\root.crt` and runs
  `certutil -addstore -f Root` — the CA lands in the **LocalMachine**
  Trusted Root store, so Edge/Chrome and other system-store clients
  validate `https://localhost/` **with no certificate bypass**.
- **Firefox** uses its own trust store — the one unavoidable manual
  step: import the same `root.crt` (Settings → Certificates → Import),
  or use a system-store browser.
- Leaf renewal: automatic — Caddy renews short-lived leafs while
  running; the root CA (~10 y) outlives the install.
- Upgrade: `caddy-home` persists under `%ProgramData%` → same CA →
  trust carries over.
- Uninstall: `untrust_caddy.cmd` removes **this** installation's CA by
  thumbprint (other Caddy CAs untouched). Customer data under
  `%ProgramData%\TestOrchestrator` is kept unless explicitly confirmed.

## Stop / restart / uninstall

- Supported stop: `%ProgramData%\TestOrchestrator\stop_to.cmd` — ends
  all four tasks **and** reaps orphaned child processes. Plain
  `schtasks /End` only kills the `.cmd` host and leaves python/caddy/
  dagu children bound to their ports — do not use it alone.
- Logs: `%ProgramData%\TestOrchestrator\logs\{web,worker,dagu,caddy}.log`
  (Task Scheduler provides no stdout capture; wrappers append here).
- Uninstall via Add/Remove Programs: untrusts the CA, stops services,
  deletes tasks and `%ProgramFiles%` payload, then asks before touching
  `%ProgramData%` data.

## Remote access — separate deployment mode

Local `https://localhost` needs none of this. For LAN/remote clients:
- Install with `/ADDR=<hostname>` so generated config/redirects use it.
- The internal CA issues a leaf for the configured name only — remote
  clients need the same root.crt imported, or provide a real
  CA-issued cert via the admin Network & certificates page (`tls` file
  mode), which is the recommended production posture.
- Firewall: inbound 443 (and 8080 if the HTTP→HTTPS redirect is used)
  must be opened by the client administrator.

## Remaining validation gates (honest status)

Verified on disposable CI (`windows-latest`): silent install, payload,
task registration+start, HTTP readiness, task restart, HTTPS with real
certificate validation, licence negative/positive/persistence, Allure 3
report generation. **Pending on an interactive Windows host:** GUI
installer screens, real-browser activation flow, actual reboot
persistence, upgrade-in-place, and uninstall UX.
