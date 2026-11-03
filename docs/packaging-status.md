# Packaging status — release bundle

Asset: `dist/testorchestrator-1.9.0-linux-x86_64.tar.gz` (+ manifest.json,
built by `scripts/build_bundle.py`). Platform tested: Linux x86_64.

## Bundled components (verified in asset)
- Application source, migrations, deploy units, docs
- **30 vendored wheels** (`3rdparty/python-wheels/`) — verified installable
  with `pip --no-index` on a clean venv (no registry access)
- Caddy 2.9.1 binary (SHA-256 recorded, upstream LICENSE)
- Third-party licences + notices (`3rdparty/licenses/`)

## Verified
- Fresh extraction → `pip install --no-index --find-links 3rdparty/python-wheels`
  → `import flask, waitress, git, requests, cryptography` — PASS (offline)

## Host prerequisites (unavoidable, before install)
- Python 3.12 interpreter present (embedded runtime NOT yet bundled — gap)
- `git` CLI on host (GitPython shells out — not bundled)
- Linux x86_64; root/CAP_NET_BIND_SERVICE for ports 443/8080
- KRE installed+licensed separately by operator (registered via Settings only)

## Exclusions and reasons
- Embedded Python runtime — not yet bundled (gap)
- ReportPortal stack — container backend + images not bundled (gap);
  no container runtime provisioning implemented/tested
- Dagu — excluded pending owner decision (Option A/B unresolved)
- KRE — external by requirement; never bundled/licensed/upgraded

## NOT yet demonstrated (incomplete)
- Reboot→services-start evidence (no systemd target host tested)
- Fresh-OS offline install end-to-end (only wheel-resolution proven)
- Windows/SLES builds (not produced)
- RP image set, Git binary, runner connector bundling

Status: **partial package** — portal+deps+Caddy bundle real and
offline-verified; complete self-contained stack claim is NOT made.
