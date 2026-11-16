# TestOrchestrator 2.0.0 — release summary (pre-publish draft)

Status: **merged to `main`** — merge commit `ded0bca` (2026-11-16).
Not yet tagged or published. This document is the release summary;
do not cut `v2.0.0` until explicitly instructed.

## What 2.0.0 is

The reporting backend migration release. Historical Allure support —
both the Allure 2/Java command-line backend and the Allure 3/Node
service — is **removed**. reportingLabs 0.1.14 is the sole enhanced
report-generation backend, vendored in-repo as source (MIT,
Naveen Khunteta / Naveen Automation Labs 2026, `reporting_labs/` with
LICENSE and `_version.py`). No PyPI download, no release-time fetch,
no Node runtime.

### Breaking changes

- **Historical Allure support removed.** Allure 2 (Java CLI) and
  Allure 3 (Node) are no longer bundled or selectable; `allure`
  report-generation backend, bundled JRE/Node, and `/allure*` portal
  routes are gone (routes return 404). Dagu, Caddy, and Python
  runtime provisioning are unchanged.
- **KRE collection selector fix**: collection selectors stored as
  `.ts` (the Katalon convention for suite collections) are now
  resolved correctly — previously only `.tsc` was stripped and such
  collections failed with KRE "suite not found" (`f4c8c14`).
- **Retry of discovered-item executions fixed**: retries now preserve
  the source item, raw runner/runtime identifiers, and remote-worker
  routing — previously they degraded to the local dag path and ended
  `UNKNOWN` with no evidence (`7adba39`).

### Component versions

| Component | 1.0.2 | 2.0.0 |
|---|---|---|
| Report backend | Allure (deprecated path) | reportingLabs 0.1.14 (vendored, MIT) |
| Dagu | 2.25.8 | **2.27.0** (GPLv3, notices retained) |
| Caddy | 2.9.1 | 2.9.1 (unchanged) |
| Python runtime | 3.12 | 3.12 (unchanged) |
| Node.js / JRE | bundled for Allure | **removed** |

## Validation completed

| Gate | Result | Evidence |
|---|---|---|
| Full regression (merged tree) | **593 passed / 1 skipped** | repo + CI `rl-tests` |
| Linux automated E2E — Dagu 2.25.2 / rl wheel | PASSED @ `2a64e0e` (+ `7adba39`, `f4c8c14`) | `testorchestrator-e2e-evidence/linux-2a64e0e/` (0700) |
| Linux automated E2E — Dagu 2.27.0 / vendored rl | **PASSED** @ `93709ee` | `testorchestrator-e2e-evidence/linux-dagu227/` (0700) |
| Real orchestration path | Portal → Dagu 2.27.0 coordinator → remote worker `to-worker-227-1` (`kre=e2e-227`) → KRE 11.7.0 | executions `4fe56af3` PASSED (2 results, 12 artifacts), `bd881ce5` collection 26 results, `212d912d` mid-flight CANCELED |
| Retry + selector defect fixes | re-verified live on 2.27.0 | same evidence |
| rlabs lifecycle | gen/view/logo(346,414 B)/ZIP(879,440 B)/auth/traversal/3-gen retention/expiry-404/failed-regen preservation/restart persistence | `rlabs.zip` capture |
| Payload hygiene | no Node/Allure runtimes; `/allure` 404; vendored rl + LICENSE in payload | bundle manifest |
| Dagu contract | provision + auth + submit/status/cancel re-verified on real 2.27.0 binary | contract suite (22 tests) |

## Artifact identities

Artifacts are tied to the commit they were built from. Do not relabel.

**Final release builds** (`7c8181f` — runs 43922613571 linux /
43922613386 windows, both green incl. `installed-validate` and
`upgrade-from-v102`; worker-asset gate active):

| Artifact | Source commit | Size | SHA256 |
|---|---|---|---|
| `testorchestrator-2.0.0-linux-x86_64.tar.gz` | `7c8181f1c65baed853fc9afc81662b6df25f2ae5` | 278,769,344 B (extracted 386,345,086 B) | `2544557e97f7dcf403476dd0155a1b98efdd6f8ad8b4db794b2981c0a0788107` |
| `testorchestrator-2.0.0-linux-x86_64.run` | `7c8181f` | 278,771,348 B | `cd61fc29754f00b9574d84a752a8d2ee902e24c16902d2bb240ae2c918701602` |
| `testorchestrator-2.0.0-windows-amd64-setup-UNSIGNED.exe` | `7c8181f` | 133,284,593 B | `f566f3de69e2187965717e2114a7fe46b0603e30b4377b0f15da59e1c7e56245` |
| `testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe` | `7c8181f` (payload: pinned `dagu_2.27.0_windows_amd64.tar.gz`) | 64,339,800 B | `24fe2ca70ba02cde44e509046bf25cabacd89f74019782637664e20029a6398c` |

### Worker-asset accounting

The Linux package carries three downloadable worker assets, all
served by the installed app (admin-gated):

- `packaging/windows/testorchestrator-dagu-worker-2.27.0-setup-UNSIGNED.exe`
  — the Windows worker installer (63,354,239→64,339,800 B across the
  Dagu bump); built via `makensis` in `linux-package` CI from the
  checksum-verified pinned windows archive. **This was the ~63 MB
  local-vs-CI gap**: local builds picked up a stale untracked 2.25.8
  exe in `packaging/windows/`; clean CI checkouts silently had none.
  Fixed by building it in CI + a `validate_package` gate.
- `3rdparty/dagu/dagu_2.27.0_linux_amd64.tar.gz` (sha256
  `a9045768c9777d0b938585696e0491bc0ce46fbeb0419989b34fde63a0cc6f85`)
- `3rdparty/dagu/dagu_2.27.0_windows_amd64.tar.gz` (sha256
  `23324e6589e64a47f51bf8a2727b18e912ec64a2eec49d4b0ed52a511f80ad46`)

Coordinator and worker versions aligned at **2.27.0**.

**Release-asset verification (2026-11-16, this machine):** the
`7c8181f` tarball was extracted and installed; the app served all
three worker assets over `/system/infrastructure/` (200, checksums
matched); a Dagu worker started **from the release asset's linux
archive** registered with a coordinator (`to-worker-rel-1`,
`kre=e2e-227`, reported v2.27.0) and executed a real dispatched KRE
job end-to-end — execution `28d1aa79` **COMPLETED/PASSED** (2
results, 12 artifacts). The Windows worker `.exe` is verified as
served + checksum-matched but not executed (no Windows host —
recorded under limitations).

**Frozen-candidate validation build** (local, `93709ee` — the tree the
2.27.0/vendored-rl E2E and 593/1 regression ran against):

| Artifact | Source commit | Size | SHA256 |
|---|---|---|---|
| `testorchestrator-2.0.0-linux-x86_64.tar.gz` | `93709ee` | 277,797,178 B (extracted 385,350,584 B) | `e3eadfaefcd50587bea70ca4d43b65e0282325e2f8e5d4a3e53d32ad07cecbf2` |
| `testorchestrator-2.0.0-linux-x86_64.run` | `93709ee` | 277,799,182 B | `c430d51ef0d53ca0b6bf3e03a1c851672b1424eefd92f5ef766946cc921fb84a` |

Note: the CI and local tarballs differ in size (214 MB vs 278 MB)
because the bundled python-runtime/wheel set is resolved per
environment; both contain identical application source, vendored
reporting_labs, dagu 2.27.0, and caddy.

### Release-artifact provenance decision

Between frozen candidate `93709ee` and merge lineage `9410cbd` the
only differences are documentation and CI workflow files — **no
payload content changed** (verified via `git diff --stat`: docs +
`.github/` only). Two honest options:

1. **Ship main-lineage CI artifacts** (recommended): built by the
   release pipeline from the merged commit, validated by
   `installed-validate` + `upgrade-from-v102` jobs; manifest records
   the merge-lineage commit. Validation basis: E2E + regression on
   functionally identical `93709ee` payload.
2. Ship the locally-built `93709ee` artifacts labeled as the
   frozen-candidate validation build.

Either way, each published artifact is labeled with its actual
source commit; nothing is described as a merge-commit build unless
it was built from one.

## Accepted limitations (owner waivers 2026-11-16)

- **Windows E2E** — not performed here (no Windows host/KRE/worker);
  owner will validate on their Windows environment.
- **Windows interactive GUI acceptance** — deferred; manual,
  owner-side.
- **Sanitised real-corpus parity / generation performance /
  report-output-size** — deferred pending corpus handoff.
- Linux worker-asset composition differs from the app bundle
  (worker installer ships only `dagu.exe` + licence/readme — no
  coordinator, scheduler, or UI components) — accounted above.
- Windows worker installer verified as served + checksum-matched but
  not executed — no Windows host available (covered by the deferred
  Windows validation waiver).

## Deferred to 2.0.1 (out of 2.0.0 scope)

- **Repository-result import** — importing KRE report folders that
  were produced outside an orchestrated run.
- **Duplicate prevention** — deduplication of imported/ingested
  results.

## Environment notes recorded during acceptance

- `d46e1e2c` failed pre-dispatch on a stray `/tmp/dagu` regular file
  colliding with Dagu 2.27.0's default temp dir — environment quirk,
  not a product defect (retained as evidence).
- `9647abd5` (earlier leg): harness misconfiguration
  (`TO_SECRET_KEY_FILE` vs `TO_SECRET_KEY`) — not a product defect.
- Desktop suite failures: test-project defect — `Desktop.ts` carries
  committed merge-conflict markers.
- Pre-fix `UNKNOWN` executions `084f5b77` / `044dd578` retained as
  defect evidence; not relabelled.
