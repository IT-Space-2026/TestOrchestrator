# Redistribution review — Buildbot stack in TestOrchestrator 2.5.0

Prepared for the approval gate `TO_BUILDBOT_GPL_APPROVED`. No
customer distribution before this plan is approved; the flag stays
unset until then.

## 1. Code boundary — how TestOrchestrator and Buildbot interact

The products run as **separate processes under separate Python
environments**, communicating only over documented wire protocols:

```
portal process (app venv)                buildbot master (buildbot-venv)
  testorchestrator/                        buildbot 4.3.0
    adapters/buildbot.py ──HTTP/REST──▶ :8010 loopback only
    dispatch.py ───────────┐
                           │ buildrequest + exec_spec (JSON)
buildbot workers (buildbot-venv / remote venv)
  to-local, to-ssh-dispatcher, remote workers ──PB/TCP :9989──▶ master
    worker executes shell steps; for SSH targets it invokes
    to_ssh_exec.py (TO source) as a subprocess
```

Boundary facts:

- The portal **never imports or links** Buildbot code — `adapters/
  buildbot.py` speaks the public REST API (`api/v2`) over loopback;
  Buildbot's own UI/API stay bound to `127.0.0.1`.
- Workers communicate with the master over PB (Perspective Broker,
  TCP 9989) with per-worker passwords — a documented inter-process
  protocol, not in-process API use.
- `paramiko` is used by `to_ssh_exec.py`/`ssh_check.py` — TestOrchestrator
  code invoked **as a subprocess** by the worker. No paramiko code is
  imported into the portal process either (it lives in buildbot-venv).
- No Buildbot source is modified, patched, or vendored-with-changes —
  we ship **verbatim upstream wheels**.

This is an aggregate-of-separate-programs posture: distinct
processes, distinct interpreter environments, wire-protocol IPC —
not a combined/derived work in the GPL sense. The obligations below
still apply to the Buildbot/paramiko packages themselves on
redistribution, which is what this review covers.

## 2. What ships in each artifact

| Artifact | Buildbot/paramiko content | Notes |
|---|---|---|
| `testorchestrator-2.5.0-linux-x86_64.tar.gz` / `.run` | `3rdparty/buildbot-wheels/` — pinned upstream wheels: `buildbot==4.3.0` (+ `[bundled]` extras), `buildbot-worker==4.3.0`, `buildbot-www==4.3.0`, `paramiko==2.12.0` + resolved deps (twisted, autobahn, zope.interface, cryptography, cffi, txaio, automat, incremental, msgpack, pyopenssl, hyperlink, idna, constantly, pycparser, six, bcrypt, pynacl, ujson, packaging, typing-extensions, setuptools deps) | install.py builds `payload/buildbot-venv` offline from these wheels; master + `to-local` + `to-ssh-dispatcher` run from it |
| `TestOrchestrator-Buildbot-Worker-Setup-4.3.0-UNSIGNED.exe` (inside the Linux package, served by the portal) | embedded Python runtime + `buildbot-worker==4.3.0` wheel + deps + `setup-worker.py` bootstrap | offline install on the remote Windows host; no controller, no KRE, no credentials inside |
| `install_linux_worker.sh` | references `bb-wheels/` or PyPI for `buildbot-worker==4.3.0` | runs on the remote host; the script itself is TO code |
| App payload (all) | **no** Buildbot code — the portal venv contains none of these packages | `paramiko` absent from the app venv; it belongs to buildbot-venv |

## 3. Licenses of the bundled packages

| Package | Version | License | Redistribution duties |
|---|---|---|---|
| `buildbot` | 4.3.0 | **GPL-2.0** (COPYING in dist-info) | license text + copyright + corresponding source |
| `buildbot-worker` | 4.3.0 | **GPL-2.0** | same |
| `buildbot-www` | 4.3.0 | **GPL-2.0** (classifier; buildbot project-wide) | same |
| `paramiko` | 2.12.0 | **LGPL-2.1+** | license text + source offer + no-TPM note (unmodified wheel — relinking trivially possible) |
| twisted | 26.10.0 | MIT | notice only |
| autobahn | 26.11.1 | MIT | notice only |
| zope.interface | 8.7 | ZPL-2.1 | notice only |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | notice only |
| cffi / pycparser | 2.1.1 / 3.12 | MIT-0 / MIT | notice only |
| automat, incremental | 25.4.16 / 24.11.0 | MIT | notice only |
| msgpack | 1.2.4 | Apache-2.0 | notice only |
| pyopenssl, hyperlink, idna, constantly, pynacl, bcrypt, txaio, ujson, typing-extensions, packaging, six, PyYAML, pyjwt | resolved at staging | MIT/Apache/BSD | notice only |

## 4. Notices and corresponding-source delivery — as implemented

**Primary route: GPLv2 §3(a) — source accompanies the binaries.**

- `build_corresponding_sources()` (build-scripts/_common.py) emits
  `TestOrchestrator-Buildbot-Sources-<ver>.tar.gz`: sdists of the
  GPL/LGPL-covered packages (`buildbot`, `buildbot-worker`,
  `buildbot-www`, `paramiko` — `--no-binary` source distributions,
  no-deps) + `build-and-install/` (provision_buildbot.py, master.cfg,
  install_linux_worker.sh, setup-worker.py, the NSIS script) +
  `CORRESPONDING-SOURCES.txt`. It is published **beside the
  controller package** and copied into `packaging/windows/` where
  the Admin UI serves it **next to the worker .exe download**
  (`system.buildbot_sources_download` + a Source link on the
  download card).
- License texts are extracted from every staged wheel into
  `3rdparty/licenses/` (controller bundle) and into
  `bb-worker-payload/licenses/` (inside the .exe) — notices travel
  with both artifacts.

**Residual route: GPLv2 §3(b) written offer** — retained only for
copies distributed *without* the archive (e.g., a customer shares
the .exe onward, or an ad-hoc transfer). `CORRESPONDING-SOURCES.txt`
carries the offer; **fulfillment owner: IT-Space Kft. — the support
contact published with the product (About page / Admin Manual)**.
Terms per GPLv2: valid ≥3 years, to any third party, at no more
than the physical cost of source distribution. The offer names the
published support channel — the release must not ship without that
contact present.

**Boundary remains as §1** — no linking, no modified sources; the
§3(a) archive makes verification direct (no upstream-link reliance).

**Inventory verification** (`validate_package.py`): asserts the
worker .exe, the controller wheels dir incl. `buildbot-www`, and —
added with this change — the sources archive + license files.
The .exe payload additionally carries embed-Python + pywin32 +
the resolved win_amd64 wheels; the review of the actual staged
artifacts happens at RC-build time per docs/acceptance-2.5.0.md.

## 5. Approval checklist

- [ ] Confirm the separate-process/wire-protocol boundary is the
      intended posture (it is — no linking, no modified sources)
- [ ] Accept the GPLv2 §3(a) accompanying-source route (archive ships
      beside every binary offer) with the §3(b) written offer retained
      only as residual for onward copies
- [ ] Approve notice files above
- [ ] Then: `TO_BUILDBOT_GPL_APPROVED=1` for the RC build only
