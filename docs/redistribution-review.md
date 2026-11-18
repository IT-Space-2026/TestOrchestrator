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

## 4. Proposed notices and corresponding-source delivery

1. **License texts** — at wheel staging
   (`fetch_buildbot_linux_wheels` / `stage_buildbot_worker_payload`),
   extract each wheel's `dist-info` license into
   `3rdparty/licenses/` alongside the existing per-package files.
   Required minimum: `GPL-2.0` full text once, plus per-package
   notices.
2. **`3rdparty/CORRESPONDING-SOURCES.txt`** (this PR adds the file) —
   names every bundled third-party package + exact version +
   upstream source location (PyPI project / git tag). GPLv2 §3(b)
   written offer: *"Corresponding source for the GPL-2.0 components
   (buildbot 4.3.0, buildbot-worker 4.3.0, buildbot-www 4.3.0) and
   the LGPL-2.1+ component (paramiko 2.12.0) is the unmodified
   upstream source at the versions listed, available from the
   locations below; a written offer valid three years from delivery
   is available on request via the support channel."*
3. **`3rdparty/licenses/THIRD-PARTY-NOTICES.txt`** — append a
   Buildbot/paramiko block: name, version, license, upstream URL,
   and the unmodified-verbatim-copy statement.
4. **No source modifications** — because we ship verbatim wheels,
   "corresponding source" is exactly upstream's; hosting a mirror is
   optional, not required, but the written offer stays valid either
   way (proposed: keep the offer in the file, satisfy via support
   channel on request).
5. **Windows .exe** — same notices inside the installer payload
   (`3rdparty/licenses/` staged into the install dir by the NSIS
   script) so the GPL/LGPL terms travel with the artifact.

## 5. Approval checklist

- [ ] Confirm the separate-process/wire-protocol boundary is the
      intended posture (it is — no linking, no modified sources)
- [ ] Accept GPLv2 §3(b) written-offer route (vs. hosting source
      mirrors)
- [ ] Approve notice files above
- [ ] Then: `TO_BUILDBOT_GPL_APPROVED=1` for the RC build only
