# Packaging status — portal bundle with external-Java prerequisite

Asset: `dist/testorchestrator-1.9.0-linux-x86_64.tar.gz` + `.sha256` +
manifest (`scripts/build_bundle.py`). `dist/` stays out of Git; the
archive is published as a release asset with its checksum, dependency
requirements, and installation instructions.

## Component-by-component completion table

| Component | Bundled | Offline install verified | Missing |
|---|---|---|---|
| App + migrations + docs | yes | yes | — |
| Python runtime 3.12.11 (python-build-standalone, linux x86_64) | yes | yes — venv created + wheels installed, no host python | Windows/SLES runtime |
| 30 vendored wheels | yes | yes (`pip --no-index`) | — |
| Caddy 2.9.1 + LICENSE + sha256 | yes | binary verified | per-OS config test |
| Runner connector (`testorchestrator/agent.py` + API) | yes (source) | in-process tests | live remote-host run |
| Git runtime | not bundled | Dulwich default; `TO_GIT_BACKEND=cli` uses host git or `acquire_git.py` re-stage (rootfs not tracked in repo) | — |
| Dagu 2.25.8 | yes | contract test + fixture chain green | — |
| Allure generator (allure-commandline 2.46.1) | yes | real generation verified vs host Java | — |
| Java runtime | **no — operator prerequisite** | validated by actual report-generation probe at install (`--java-home` / detection / `--no-allure`), re-checkable in Admin → Reporting | — |
| ReportPortal stack/images/config | **no** | — | container backend + pinned images |
| KRE | **no** (external by requirement) | — | operator-installed |
| Installer/launcher (config/services/HTTPS) | partial `scripts/install.py` + `create_venv_offline` | dry-path only | full lifecycle on target |
| Reboot service start | no | — | needs systemd target test |
| Windows 11 / SLES packages | no | — | not produced |

## Java / Allure packaging decision

The JRE is NOT part of the standard distribution (removed from the
package and from unpublished Git history; the acquisition manifest +
licence set under `3rdparty/java-runtime/` is retained as the pinned
reference for a separately supplied runtime, and `acquire_git.py --root
openjdk-25-jre-headless` can re-acquire it for other purposes).

A compatible Java runtime is a declared prerequisite **only** for
Allure report generation:

- The installer detects Java (`JAVA_HOME`, PATH, `/usr/lib/jvm`) or
  takes an explicit `--java-home`.
- Validation is an actual `allure generate` probe with the pinned
  2.46.1 generator, run as the `testorchestrator` service account.
- The validated path persists in `/etc/testorchestrator/web.env`
  (`TO_ALLURE_JAVA_HOME`, app-scoped — no global Java settings change,
  KRE's runtime untouched) plus `allure-java.json` readiness record.
- Admin → Reporting shows the selected path, Java version, generator
  version, and probe readiness; a path override there re-validates.
- No compatible Java → recheck or continue with `--no-allure` and a
  clear warning; native KRE reports and artifacts are unaffected.

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
- ReportPortal image set + config automation, WSL/container
  backend, full installer lifecycle, per-OS packages, reboot evidence.
