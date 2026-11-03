# Packaging status — PARTIAL PORTAL BUNDLE (not the self-contained installer)

Asset: `dist/testorchestrator-1.9.0-linux-x86_64.tar.gz` + manifest
(`scripts/build_bundle.py`). Label: partial portal bundle — final component
set unfrozen pending owner's Dagu decision.

## Component-by-component completion table

| Component | Bundled | Offline install verified | Missing |
|---|---|---|---|
| App + migrations + docs | yes | yes | — |
| Python runtime 3.12.11 (python-build-standalone, linux x86_64) | yes | yes — venv created + wheels installed, no host python | Windows/SLES runtime |
| 30 vendored wheels | yes | yes (`pip --no-index`) | — |
| Caddy 2.9.1 + LICENSE + sha256 | yes | binary verified | per-OS config test |
| Runner connector (`testorchestrator/agent.py` + API) | yes (source) | in-process tests | live remote-host run |
| Git implementation | **no** | — | portable Git binary not bundled |
| ReportPortal stack/images/config | **no** | — | container backend + pinned images |
| Dagu | **no** | — | excluded pending owner decision |
| KRE | **no** (external by requirement) | — | operator-installed |
| Installer/launcher (config/services/HTTPS) | partial `scripts/install.py` + `create_venv_offline` | dry-path only | full lifecycle on target |
| Reboot service start | no | — | needs systemd target test |
| Windows 11 / SLES packages | no | — | not produced |

## Acceptance procedure (fresh machine, network blocked)
1. `tar xzf dist/testorchestrator-1.9.0-linux-x86_64.tar.gz`
2. `cd testorchestrator-*/ && 3rdparty/python-runtime/python/bin/python3 -m venv venv`
3. `venv/bin/pip install --no-index --find-links 3rdparty/python-wheels -r requirements.txt`
   — VERIFIED offline on this host
4. `venv/bin/flask --app wsgi:app db upgrade && venv/bin/waitress-serve --listen=127.0.0.1:18080 wsgi:app`
5. Browse https://localhost (Caddy) or http://127.0.0.1:18080 (direct):
   bootstrap Admin → mandatory password change → Settings → Network &
   certificates → Runners & runtimes → register existing KRE path →
   "Test connection" probe → run suite on runner.

## Remaining gaps (missing implementation, not validation)
- Portable Git, ReportPortal image set + config automation, WSL/container
  backend, full installer lifecycle, per-OS packages, reboot evidence.
