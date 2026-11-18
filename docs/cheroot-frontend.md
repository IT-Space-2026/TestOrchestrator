# Cheroot HTTPS frontend (Linux opt-in)

TestOrchestrator's public HTTPS endpoint is served by the bundled
**Caddy** reverse proxy by default. On Linux, the installer also offers
an opt-in **Cheroot** tier that terminates TLS inside the application
process. Both tiers serve the same application; only the edge differs.

Status in 2.5: **supported opt-in on Linux, off by default.** Caddy
remains the default for fresh installs and upgrades. Windows builds
ship Caddy only — Cheroot on Windows is not validated and is not
selectable there. Making Cheroot the default is a separate decision.

## What the Cheroot tier covers

Exactly the Caddy responsibilities the product uses — verified in the
parity pass (`docs/spike-cheroot-frontend.md`):

- TLS termination on the public HTTPS port from the managed cert dir
  (`<data>/certs`), all three certificate modes (`internal`, `local`,
  `imported`).
- Optional HTTP→HTTPS redirect with the configured target; path,
  trailing slash and query are preserved; the inbound Host is never
  reflected into the redirect.
- Host allowlist (local_host + optional external_host + loopback
  aliases); other hosts get `421 Misdirected Request`.
- `Server` header masked to the product name.
- Client-supplied `X-Forwarded-*` headers are stripped at the edge —
  in the direct-serving tier the client is not a trusted proxy hop.
- Hot certificate reload: admin regenerate/import or any managed-file
  change takes effect on new connections within ~2 s, no restart;
  invalid replacement keeps serving the last-good context and logs
  the failure (no key material).
- Waitress keeps its loopback listener `127.0.0.1:18080` for probes —
  unchanged on both tiers.

Deliberately not provided: HTTP/2 and HTTP/3 (the tier is HTTP/1.1
only), ACME/automatic issuance (certificates are generated or
imported by the application on both tiers), listener rebind without a
service restart, Caddy's admin API.

Measured on the 2.5 candidate build (Linux x86_64, 12-core host):
steady-state HTTP/1.1 throughput is within ~10–15 % of Caddy
(~200–330 rps on `/healthz`, ~75–90 rps on an authenticated page —
app-bound on both tiers). Known limitation: under ≥40 keep-alive
connections the tail latency is higher than Caddy's
(p99 ~0.6–2 s vs ~0.3 s) because connections pin WSGI worker threads —
irrelevant at admin-portal load but real. Total resident memory is
lower than the two-process Caddy+Waitress pair (~97 MB vs ~148 MB).

## Installing with the Cheroot frontend

Fresh install (also works on an extracted package rerun):

```bash
sudo bash install.sh --frontend cheroot \
    [--payload /opt/testorchestrator --data /var/lib/testorchestrator \
     --config /etc/testorchestrator]
```

What this does differently from a default install:

- stages `deploy/testorchestrator-web-cheroot.service` as the
  canonical `testorchestrator-web.service` — it runs
  `venv/bin/python wsgi.py` with `TO_FRONTEND=cheroot` and
  `AmbientCapabilities=CAP_NET_BIND_SERVICE` for the privileged port;
- writes `cheroot` into `<config>/frontend` — the selection marker;
- stages but does **not** enable `testorchestrator-caddy.service`;
- enables `testorchestrator-web`, `-worker`, `-dagu` (caddy excluded —
  only one frontend may own the public ports).

`--frontend` accepts `auto` (the default), `caddy`, `cheroot`:

- `auto` resolves to the marker left by a previous install, else
  `caddy`. Fresh installs and 2.0.x upgrades (no marker) therefore
  stay on Caddy, and a Cheroot install keeps its tier across
  upgrades — an unattended `install.sh` rerun can never silently
  switch frontends.
- An explicit `--frontend caddy|cheroot` switches tiers and rewrites
  the marker — this is the supported way to change frontends.

## Switching and rollback

No application-data migration is involved in either direction — the
frontend is a service-selection change only; database, certificates,
settings, schedules, workers and evidence are untouched.

Cheroot → Caddy:

```bash
sudo systemctl stop testorchestrator-web
sudo bash install.sh --frontend caddy   # same --payload/--data/--config
```

The installer restages the Waitress-only web unit, enables the caddy
unit, and disables nothing else. Caddy's own CA state under
`caddy-home/` was never touched by the Cheroot tier, so `internal`
mode restores its original trust anchor.

Caddy → Cheroot: same command with `--frontend cheroot`. The
installer disables the caddy unit and restarts the web unit under its
Cheroot template; a running caddy is stopped so the ports are free.

Certificate note: `internal` mode uses a *different* trust anchor per
tier — Caddy's own CA vs. the application's Python-generated local CA
(`certs.generate_local`). Switching tiers under `internal` changes the
CA clients must trust; re-distribute `local-ca.pem` (or switch modes)
when changing frontends. `local` and `imported` modes serve the same
managed files on both tiers, so no client change is needed there.

## Platform support

| Platform | Status |
|---|---|
| Linux x86_64, systemd, unprivileged or root install | supported opt-in |
| Linux other distros/arch | not tested |
| Windows | not validated — Caddy only |
| Root system-scope on :443, reboot, upgrade from released build | release-gate checks; tracked in the 2.5 PR |

## Operational notes

- Logs: `journalctl -u testorchestrator-web` — cert reload success and
  failures (`last-good` kept) are logged there; private key material is
  never logged.
- The Services page lists and restarts the tier-appropriate unit set
  (no caddy entry under Cheroot).
- If the loopback backend fails to start, the process exits so
  `Restart=on-failure` engages — the public listener is never left
  serving a dead app.
- Cold start with invalid TLS material fails visibly (SSL error →
  process exit → systemd retry) — the app is never exposed over plain
  HTTP as a fallback.
- Backend down under Caddy: `502`; under Cheroot: connection refused
  (the single process exits) — operator-visible difference, by design.

## Third-party components

`cheroot 11.1.2` (BSD-3-Clause), `jaraco.functools 4.6.0` (MIT),
`more-itertools 11.2.1` (MIT) — vendored as wheels in
`3rdparty/python-wheels/` for fully offline installs; licence texts in
`3rdparty/licenses/` and the in-app About → Notices page.

## Package size

Linux tarball contribution: ~0.2 MB compressed (three wheels; ~1.6 MB
installed). If Caddy were later removed the package would shrink by
~17.7 MB compressed / ~50.9 MB installed — not part of this change;
Caddy continues to ship on every tier.
