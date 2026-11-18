# Validation Record — TestOrchestrator 2.0.2 documentation

How the 2.0.2 manuals were produced and what was actually verified.
Nothing here is claimed beyond what was run or read.

## 1. Package under test

| Item | Value |
|---|---|
| Release | `v2.0.2` — https://github.com/IT-Space-ltd/TestOrchestrator/releases/tag/v2.0.2 |
| Source commit | `574860d08124411692b141643da0ad87e576100d` |
| Package | `testorchestrator-2.0.2-linux-x86_64.tar.gz` |
| Size / SHA-256 | 278,808,449 B / `af08a60d1506b061edd1342169aded75553dbc3ee6b134df23c19ec7670a6f7e` — re-verified against the published `SHA256SUMS.txt` before use |
| Dagu (coordinator + worker) | `2.27.0` |
| reportingLabs | vendored `reporting-labs` 0.1.14 (MIT) |

## 2. Documentation environment (isolated)

| Item | Value |
|---|---|
| Install root | `/home/szefi/to202-docs` |
| Installer source | `install-src/` — extracted published tarball |
| Payload | `payload/` (produced by `install.sh`) |
| Data (instance) | `data/` — own SQLite DB, evidence, certs, bindings, staging |
| Config | `config/` — `web.env`, `dagu/` (server.yaml, credentials, units), `Caddyfile` |
| Web | Waitress on `127.0.0.1:8095` |
| Dagu coordinator | http `127.0.0.1:8097`, gRPC `127.0.0.1:58055` (`DAGU_HOME=data/dagu`) |
| Worker | `worker/dagu` extracted from the portal-served archive, `--worker.id=doc-worker --worker.labels="kre=doc-demo"`, health port disabled |
| Logs | `data/web.log`, `data/coordinator.log`, `data/worker.log` |
| Demo repo | `/tmp/doc-repo.git` — real Katalon-cert project + genuine KRE 11.7.0 `Reports/` output + two documentation-only additions (a failed-case variant, a late-arriving collection suite) |

**Licence note.** The production licence private key lives outside
this repository (separate issuer); no valid production-signed code
exists in this environment. For this **documentation-only install**
the payload's `release/licence_pubkey.pem` was replaced with a
generated test key and a self-signed demo licence
(`doc-manual-202`) was issued — exactly the mechanism the packaged CI
driver uses. Published binaries, the tag, checksums, and manifests
were **not** modified; the substitution exists only inside the doc
install.

## 3. Procedures — status

Legend: **EXEC** = executed and verified on this install;
**IMPL** = verified against implementation/CI evidence only;
**BLOCKED** = not validated (reason stated).

### Installation & setup

| Procedure | Status | Result |
|---|---|---|
| Download package + `sha256sum -c` vs published `SHA256SUMS.txt` | EXEC | `OK` on the tarball |
| `install.sh` unattended: licence gate → payload copy → offline venv → dagu provision → unit staging | EXEC | Completed; licence validated; units staged (non-root, `--no-service-start`) |
| `install.sh --dry-run` | IMPL | Flag present in installer code |
| `.run` self-extracting package | IMPL | Same payload as tarball; checksum published; not executed separately |
| `toctl db-upgrade` on fresh instance | EXEC | Migrations applied through `r8s9t0u1v2w3` |
| Bootstrap `Admin` creation + `flask set-admin-password` + `seed-users` | EXEC | Admin + manager + tester created; setup marked complete |
| Waitress start + login flow (UI) | EXEC | Login → dashboard at 1440×900 |
| systemd unit activation | IMPL | Units staged; activation requires root/systemd — documented, not run |

### Usage (user manual)

| Procedure | Status | Result |
|---|---|---|
| Binding sync + discovery on real Katalon project | EXEC | `94 cases · 14 suites · 3 collections · 1 profile` |
| Import preview → confirm (two real-format runs) | EXEC | `new`/`new` → imported `efe2255b` (passed) + `ed860c52` (failed) |
| Duplicate detection | EXEC | re-preview → `duplicate` candidates with matched executions (screenshot) |
| Late collection-suite supplement | EXEC | `supplement` → suite-key union `API-TS + IT-Space2`, +2 results, +1 artifact |
| Execution deletion + owned-evidence cleanup | EXEC | stale execution deleted; `remove_owned` removed its dir |
| Pending-preview retention | EXEC | session `53646b10` left open for screenshots |
| Live remote dispatch (fixture KRE) | EXEC | `5edcdf4b` dispatched `transport=dagu-worker` → `completed`/`passed`, 3 cases, evidence in `remote-runs/` |
| reportingLabs generation | EXEC via workaround | `ready` on all three executions — **see §4 defect** |
| Cancel / retry / schedules / releases | IMPL | Routes and code reviewed; not exercised in the doc install |

### Administration (admin manual)

| Procedure | Status | Result |
|---|---|---|
| Worker archive download via `/system/infrastructure/dagu-worker/…` | EXEC | 64,492,882 B, SHA-256 `a9045768…` = pin |
| Worker registration + label dispatch | EXEC | `doc-worker` healthy, `kre=doc-demo`, v2.27.0; selector `map[kre:doc-demo]` matched |
| Coordinator provisioning (`provision_dagu.py`) + basic-auth API | EXEC | `server.yaml` + `credentials` 0600; `/api/v1/workers` answered |
| `toctl evidence-prune` dry-run + `--apply` | EXEC | dry-run listed seeded orphan, removed nothing; `--apply` removed only the orphan |
| Referenced-evidence protection | EXEC | live execution's evidence never listed as orphan |
| `toctl backup` / `toctl recover` | IMPL | reviewed: atomic `.part` publish, retention cap, restore-to-separate-dir semantics; archive format verified in code |
| 2.0.1 → 2.0.2 upgrade (released 2.0.1 → this package) | EXEC (earlier candidate builds; re-verified on `3c98ed5`) | `p6q7r8s9t0u1 → q7r8s9t0u1v2 → r8s9t0u1v2w3`; history/settings/evidence retained — recorded in `docs/release-notes-2.0.2.md` |
| Caddy / TLS / systemd services | IMPL | config rendered + units staged; not started (non-root doc install) |
| Windows app + worker installers | IMPL | `windows-installer` CI green on `574860d` (compile, installed-validate, upgrade-from-v102, upgrade-from-v200); unsigned; not run locally |

## 4. Defect found during documentation (honest disclosure)

**`scripts/install.py` payload copy omits `reporting_labs/`.**
The vendored package ships at the tarball root and is included in the
Windows payload list (`build_windows.py`), but the Linux installer's
copy list skips it. Consequence: on an `install.sh`-installed Linux
system, reportingLabs generation fails in the serving process —
`rl_status=failed`, log shows `No module named 'reporting_labs'`.
Confirmed on this doc install through the real web route
(`POST /executions/<id>/rlabs/generate` → `failed`).

- **Not a 2.0.2 regression:** 2.0.1's installer has the identical
  copy list. It also explains why earlier E2E runs reported `ready`:
  the driver ran from the tarball root, where the module resolves.
- **Doc-install workaround used here:** generation invoked with
  `PYTHONPATH=<tarball-root>` so `import reporting_labs` resolves;
  the generated report is then served normally by the running app
  (screenshots show real UI reading real generated output).
- **Customer-visible workaround** (documented in Admin Manual §10):
  copy `reporting_labs/` from the extracted tarball into the payload
  directory, then Regenerate. **2.0.1/2.0.2-scoped:** apply this only
  on those released versions — unnecessary from 2.0.3 onward.
- **Fix status (post-documentation):** corrected in 2.0.3 (branches
  `fix/installer-reporting-labs` → `integration/2.0.3`) — the installer
  copies the vendored package (relocated to
  `3rdparty/reporting_labs` in both the package and the installed
  payload), aborts on any missing payload source, verifies required
  files after copying, and the app resolves `reporting_labs` through
  a scoped bootstrap in `testorchestrator/__init__.py` — no manual
  copy or PYTHONPATH. Upgrading also removes a superseded root-level
  `payload/reporting_labs` copy so the package exists exactly once.
  Regression tests cover copy-list completeness, import resolution
  from the installed payload, no masking by a stale site-packages/repo
  copy, and upgrade repair of a defective 2.0.2 install.

## 5. Explicit limitations retained

- **Licensed-KRE execution remains blocked** — no Katalon licence/API
  key on this host. The fixture KRE only demonstrates pipeline
  behaviour (download → register → dispatch → execute →
  result/evidence return → report generation).
- **Real KRE activation failure** was recorded earlier (exit 2, "no
  offline licenses") — it is a blocker record, not a passed test.
- **Windows interactive procedures are not locally validated** —
  installer + CI evidence only; both Windows binaries are unsigned.
- **KRE threshold declarations** (`retry`, `retryStrategy`,
  `maxFailedTests` minimum versions) and the **Azure DevOps
  repository gate** remain pre-existing backlog items, unverified.
- Screenshots exclude passwords, keys, tokens, and licence details;
  all identities shown are demonstration-only.

## 6. Environment lifecycle

Left running for review: web `:8095`, coordinator `:8097`/`:58055`,
worker `doc-worker`. Stop and clean up:

```bash
kill $(cat /home/szefi/to202-docs/web.pid) \
     $(cat /home/szefi/to202-docs/worker.pid) \
     $(cat /home/szefi/to202-docs/coordinator.pid)
# data, config, payload under /home/szefi/to202-docs may be deleted;
# /tmp/doc-repo.git and /tmp/doc-repo-wc are scratch demo repos.
```

Existing development/test/production installs were not modified.

## 7. Follow-on validation — 2.0.3 (`be5d133`)

After the licence-label and scope-picker fixes, the 2.0.3 package was
rebuilt from `be5d1335bb82e9e49566a4d1f42fa710dd2cf6de` and the tag
moved to that commit. Verified on this environment:

| Procedure | Status | Result |
|---|---|---|
| Fresh install of the be5d133 tarball to `/tmp/to203-fix-*` | EXEC | Installer transcript captured (`admin/install-transcript.png`); licence validated; payload/data/config laid out |
| **In-place upgrade of this doc environment** (2.0.2-era payload → be5d133 build) | EXEC | `install.sh` on same `--payload/--data/--config` preserved every record: suite, release + 2 scope items, schedule, 112 discovered items, users, licence. Services restarted on the same ports |
| **Backup → restore** of the live data dir | EXEC | SQLite online backup + file copy → restored instance booted at `:8099` — sign-in, execution history, release scope, licence all intact (`admin/dr-restored-*.png`) |
| `toctl evidence-prune` dry-run | EXEC | JSON `{orphans: [], removed: []}` — `admin/evidence-prune.png` |
| Screenshot recapture on 2.0.3 | EXEC | `licence.png` (corrected expiry wording), `release-detail.png` (scopeable-only picker), `admin/install-*.png` (fresh-install frames) |

Same boundaries: fixture data inside this documentation payload only;
the doc env now runs the **be5d133** payload (all prior
2.0.2 evidence remains as recorded above — provenance is per-frame in
the Screenshot-Index). Additional throwaway instances running for the
new captures: web `:8098`/`:8099`, coordinator `:29877`.
## 8. Follow-on validation — 2.5.0 (`1adf71b8`)

The 2.5.0 candidate was built from `1adf71b8d5f8`
(`feature/cheroot-frontend`) and exercised on a disposable isolated
install (`/tmp/to-e2e2`, user-scope — no root, no VM). What was
verified there:

| Procedure | Status | Result |
|---|---|---|
| Fresh install, no `--frontend` | EXEC | resolves to **Caddy**; marker `config/frontend=caddy`; `web.env` written with `TO_SECRET_KEY`/`TO_INSTALL_ID` |
| No-flag installer rerun (×3, across builds) | EXEC | `[dagu] reusing already-provisioned coordinator credential`; `web.env` secrets preserved; credential hash unchanged; dispatch works with no resync |
| Caddy → Cheroot → Caddy switch | EXEC | only the selected tier owns the public ports; redirects + healthz verified; sessions survive (`TO_SECRET_KEY` preserved); coordinator credential hash identical before/after each switch |
| Real dispatch via UI on each tier | EXEC | UI → outbox → worker → Dagu run → agent claim → real KRE `katalonc` 11.7.0 invocation → portal state + `kre-invocation/stdout/stderr` evidence. Runs `93ba940c`, `0160e56f` (Cheroot), `a900c20f` (post-rollback Caddy) — all licence-gated `failed` (see blocked item) |
| Explicit runner/runtime pick under Dagu | EXEC | `runtime_id` honored by the dispatch path — stub-runtime run `30e27bfa` assigned the selected runtime |
| Cancellation, controlled runner | EXEC | `30e27bfa`: `cancel_requested` → Dagu run `aborted` → terminal `canceled` in ~4 s; audit shows `execution.cancel_requested` + `execution.cancel_acknowledged` |
| Assignment-before-submit ordering | EXEC | no run stranded `UNKNOWN` since the fix; the earlier reproduction (`567b446d`) is superseded by assignment-committed-before-submit + `tests/test_dagu_dispatch_ordering.py` |
| Branded 404, both tiers | EXEC | authenticated 404 renders the IT-Space-branded error page with status 404; anonymous 404 keeps status 404; logo asset decodes in-DOM |
| Foreign `Host` header | EXEC | Cheroot **421**; Caddy passes through to the app — intentional asymmetry, not weakened |
| Backend down | EXEC | Cheroot: connection refused (no HTTP page); Caddy: 502 unbranded. No branded 500/backend-down page exists — not claimed in the manuals |

**Blocked:** a *successful* real KRE run on each frontend — the probe
KRE 11.7.0 activates with **0 valid offline licences** and no
recoverable API key; every real run reaches `katalonc` and fails at
activation (`exit 2`, `infrastructure`). Requires a licensed test
environment. Cypress/Playwright/Robot Framework: **not applicable** —
not shipped in 2.5.0.

**Not exercised:** root/system-scope `:443`, real reboot, root-required
`installed-validate` steps, Windows Cheroot. System-scope upgrade was
dropped from the checklist — 2.0.3 was never deployed, so no upgrade
path is claimed for 2.5.0.
