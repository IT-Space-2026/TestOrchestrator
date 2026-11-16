# Reporting migration — Allure 3 → reporting-labs (2.0)

Branch: `feature/2.0-reporting-labs` only. Released v1.0.1 and `main`
ship Allure 3 unchanged. The reporting-labs backend is an optional
second enhanced reporter — Allure remains the default
(`report_backend=allure`) until parity acceptance below.

## Status (as of the portal-integration milestone)

- `reporting-labs==0.1.14` — dev/test dependency ONLY
  (`requirements-dev.txt`). Not in `requirements.txt`, not packaged,
  not in installers.
- Adapter: `testorchestrator/adapters/reporting_labs.py` — canonical
  Execution/TestResult/Artifact rows → single-file
  `reporting-labs/index.html`. Same honesty contract as the Allure
  adapter (result_id-first attachment mapping, unambiguous filename
  fallback, unmatched evidence labelled, unresolved never claimed
  passed).
- Portal: `/executions/<id>/rlabs`, `/rlabs.zip`,
  `/rlabs/generate` — `project_allowed()`-gated like every other
  report surface; CSP `sandbox allow-scripts allow-same-origin`.
- Failure semantics: failed regeneration sets `rl_status=failed`,
  preserves and still serves the previous usable report with an
  explicit warning. No silent fallback.
- Concurrency: unique `mkdtemp` build dirs + per-file `os.replace`
  publish — readers never see a missing/partial `index.html`.
- Unresolved outcomes: portal banner on the execution page counts
  them from canonical results ("N unresolved displayed as skipped by
  this reporting backend") — RL aggregates cannot hide the
  discrepancy.
- Run-level evidence: `kre-*.log` artifacts surface as labelled
  global output + a distinct run-level record; never case-attributed.

## Consumer audit — bundled Node.js + Allure 3

**Generation vs serving**: every Node/Allure dependency identified in
this audit exists ONLY to produce NEW reports. **No Node dependency
was found in the historical report-serving path** — the serving routes
are static file I/O. A packaged-install check (retained reports +
assets served with Node/Allure absent) is still required before
removal; `test_historical_allure_serving_needs_no_generator` covers
the code path, not the packaged artifact.

| File / location | Purpose | Needs Node/Allure | Action on removal |
|---|---|---|---|
| `testorchestrator/allure_runtime.py` | bundled node+allure discovery, probe, `generate()` subprocess | yes — generation | delete with the backend |
| `testorchestrator/adapters/allure.py` | canonical results → allure-results → cli.js | yes — generation | delete with the backend |
| `testorchestrator/readiness.py` (~L127) | allure availability in readiness status | generation probe | drop Allure section |
| `testorchestrator/cli.py` `allure-probe`, `--no-allure` | install/diag probe | generation probe | drop/swap commands |
| `testorchestrator/system/routes.py` reporting page + `/reporting/check` | admin diagnostics + probe | generation probe | repoint at reporting-labs check |
| `testorchestrator/settings.py` `allure_enabled`, `allure_probe` | backend flag + probe record | n/a — settings rows | keep (harmless) or migrate |
| `testorchestrator/execution.py` `allure_status/path/version` | per-run report state | **serving** — keeps historical reports addressable | KEEP columns forever |
| `portal/routes.py` `/allure*` view/asset/zip routes | hosted + downloaded historical reports | **serving** — pure file I/O | KEEP forever |
| `portal/routes.py` `/allure/generate` | regen action | generation | gate on backend or remove |
| `templates/execution_detail.html`, `system/reporting.html` | UI | n/a | keep view links; update settings |
| `scripts/build_bundle.py` | provisions `3rdparty/{nodejs,allure3}` + checksums | packaging | drop components after acceptance |
| `packaging/windows/build_app_installer.sh`, `check_payload.sh` | node zip download, payload asserts | packaging | drop |
| `packaging/pyinstaller/app.spec` | hiddenimports `adapters.allure` | packaging | swap for reporting_labs |
| `scripts/install.py --no-allure`, `_allure_version` | install gate + version record | packaging | drop |
| `scripts/validate_package.py` | real node+allure generate probe | packaging | swap probe for RL generation |
| `.github/workflows/{windows-installer,linux-package}.yml` | npm ci, node download, generation checks | packaging/CI | update checks |
| `docs/{DEPLOYMENT,component-manifest,artifact-manifest,install-verification}.md` | docs | docs | update (some already stale) |
| `tests/test_allure_serving.py` | serving authz/traversal/zip | **serving** — keep green | KEEP forever |
| `tests/test_allure_reporting.py` | generation tests | generation | retire with backend |

**Non-reporting Node consumers: none found.** git (git_backend/
git_runtime), caddy, dagu, kre, authoring use their own tooling —
Node is exclusively the Allure generator engine.

## Report publication semantics

Output is **not** a single file in general: `index.html` + `report.json`
+ optional `assets/` (attachments above the 2 MB embed limit are
externalised). Publication is therefore generation-atomic, not
file-atomic:

- Each generation builds in a unique `reporting-labs.tmp.*` dir, then
  lands as one complete `reporting-labs/gen-<id>/` via atomic rename.
- `reporting-labs/current` flips to the new generation via atomic
  `os.replace` — the visible report changes in ONE step.
- `rl_path` always references one complete generation dir
  (`reporting-labs/gen-<id>/index.html`); the zip download bundles
  exactly that dir — never a cross-generation blend.
- The whole publish (rename, pointer flip, prune, DB commit) runs under
  a per-evidence-dir `.publish.lock`: concurrent regenerations are
  serialised, last writer wins whole — a slower writer cannot prune or
  overwrite the winner's generation (defect found + fixed by test).
- Bounded retention keeps the newest **three** generations
  (`_RETAIN_GENERATIONS`), so a reader holding generation N still
  resolves its `report.json` + `assets/` while TWO newer generations
  publish; it is pruned on the third. This is bounded continuity, not
  unlimited active-reader protection.
- Hosted serving is **generation-scoped**: `view_rlabs` injects
  `<base href=".../rlabs/<gen>/">` into the served `index.html`, and
  `/rlabs/<gen>/<path>` only resolves files inside an exact
  `gen-[0-9a-f]{12}` dir (contained via `safe_under`; `..`, `current`,
  and malformed segments 404). A stale reader therefore always loads
  files from ITS OWN generation — never silently substituted with a
  newer generation's file. Once the generation is pruned (N+3), the
  route returns a plain-text 404 telling the reader to reopen the
  report page for the current version; reopening serves `rl_path`,
  which always points at the recorded (retained) generation.
- A failed emit or a failed publish leaves `rl_status=failed` with
  `rl_path`/`current` untouched: the last complete generation remains
  referenced, servable, and never orphaned.

### Boundary coverage (test → guarantee)

| Boundary | Test | Status |
|---|---|---|
| Unique tmp dirs / no clobber | `test_unique_tmp_dirs` | green |
| Simultaneous regen, consistent end state | `test_simultaneous_regeneration_consistent` | green — found+fixed a real interleaved-writer defect (publish lock added) |
| Reader never sees missing/partial report | `test_reader_never_sees_missing_or_partial_report` | green — resolves `current` like the route resolves `rl_path` |
| Whole-generation consistency (no mixed files) | `test_generations_are_distinguishable_and_not_mixed` | green — marker content proves single-gen bundles |
| Mid-publish failure keeps last generation | `test_publish_failure_midway_keeps_last_generation` | green — pointer swap failure simulated |
| In-flight page from gen N through TWO newer publications | `test_previous_generation_files_survive_regen` | green — newest-three retention; gen-N `index.html`/`report.json`/`assets/` still resolve; pruned at N+3 |
| Stale reader never served another generation's files; expired gen gets clear 404 | `test_rlabs_serving_is_generation_scoped` | green — base-href scoped to recorded gen; gen-scoped file route; expiry message |
| Crash between FS publish and DB update | `test_db_crash_after_publish_leaves_servable_state` | green — old rl_path still serves; next regen resyncs |
| Windows lock path (`msvcrt`) | `windows-locking` CI job on windows-latest | green on real Windows (flock→msvcrt branch, atomic rename + `os.replace` pointer flip) |
| Historical Allure serving w/o generator | `test_historical_no_runtime.py` + `historical-no-runtime` CI job (ubuntu+windows) | green — generator physically absent (CI checkouts ship no 3rdparty payloads) |

### RESOLVED — active-reader retention policy

Chosen policy: **bounded count retention** — `_RETAIN_GENERATIONS = 3`,
the newest three generations per report are kept (mtime-ordered; the
just-published generation is kept unconditionally). Generation-specific
asset paths already exist: every generation is a self-contained
`gen-*/` dir (`index.html` + `report.json` + `assets/`), so a stale
reader fetches only files inside its own generation.

Acceptance covered by `test_previous_generation_files_survive_regen`
and `test_rlabs_serving_is_generation_scoped`: a reader holding
generation N — verified on disk including a >2MB externalised
`assets/` attachment — still resolves `index.html`, `report.json`,
and its asset files while generations N+1 **and** N+2 publish; the
files are pruned only at N+3. Serving is generation-scoped end to end
(injected `<base>` + gen-locked file route), so a stale reader can
never receive a different generation's files; an expired generation
returns an explicit "reopen the report page" 404. Bounded trade-off:
a reader three-or-more generations stale must reopen — this is
bounded continuity, not unlimited protection. Full regression on the
final candidate before merge remains a standing gate.

### Historical-serving correction (found by installed-package check)

Two latent defects in the **Allure** path were found while building this
check — present on v1.0.1, fixed on this branch only:

1. `safe_under()` used a POSIX-only `str(root) + "/"` prefix check — on
   Windows EVERY report-serving route (native, Allure, zips) 404'd.
   Separately fixed for the released line in PR
   `fix/safe-under-windows` → **v1.0.2 warranted** (publication pending
   owner approval; v1.0.1 tag/assets unchanged).
2. A failed regeneration set `allure_status=failed` while
   `view_allure`/assets/zip gated on `status=="ready"` — the historical
   report became unservable. Now mirrors reporting-labs: `failed` keeps
   serving the previous usable report with a warning flash, and the
   missing-inputs path no longer downgrades a usable report.

Remaining open boundaries for acceptance: a packaged install
(Windows+Linux) demonstrating retained Allure reports work with no
Node/Allure payload, and the same publication guarantees under the
installed Windows service account — pending corpus acceptance.

## Count reconciliation

Canonical results and report headline totals are DIFFERENT sets:

- **Canonical test-case outcomes** come from `test_results` rows
  (JUnit-normalised): the demo's 7 cases → 2P/3F/1E→F/1S in RL terms.
- **Unresolved cases** (`unresolved=True`) are displayed as skipped
  with an explanatory error — never counted as passed. The portal
  banner reports their canonical count separately.
- **Synthetic evidence records** (no test-case counterpart):
  `[unmatched run-level artifacts]` and `[run-level logs]` appear as
  skipped test entries purely to carry evidence — they inflate the
  report's Tests/Skipped tabs.

Demo reconciliation: 7 canonical cases → RL "Tests: 9" (7 + 2
synthetic records), "Skipped: 4" (1 genuine skip + 1 unresolved + 2
synthetic). Headline totals are the report's own accounting — use the
portal Results table + unresolved banner for canonical counts.

## Reproducible demo evidence

`scripts/rl_demo_stage.py` stages a disposable instance (mkdtemp +
sqlite + test-key licence), seeds a synthetic 7-case execution with
mixed outcomes/evidence, generates BOTH reports, and captures
Playwright screenshots + a rendered-DOM injection check
(`window.__pwned` must be None). The `rl-tests` workflow `demo` job
runs it on every branch push and uploads `rl-demo-shots` as a workflow
artifact (30-day retention). Local runs produce the same shots under
`/tmp/rl-demo-shots*` — none of this touches real KRE evidence and the
disposable DB/licence never enters source or packages.

## Step/log coverage (evidence-based, not assumed)

KRE JUnit is test-case granularity — **no per-step data exists in the
canonical ingestion model**. Preserved local evidence (514
remote-runs) is synthetic CI fixtures only; real KRE `report.html`
step structure is unverified. RL `steps` stay empty rather than
invented. Run-level logs ARE mapped (above). If a sanitised corpus
contains richer per-step data (e.g. KRE `report.html` internals or
execution.log formats), re-evaluate then — not before.

## OWNER ACTION — sanitised KRE corpus request

Please supply, via an approved channel (NOT from protected
installations; no live KRE runs), a sanitised KRE result bundle
containing as much of the following as available:

- [ ] Executions covering passed / failed / skipped / retried outcomes
- [ ] JUnit XML (as produced by KRE)
- [ ] `report.html` / Katalon native report files (to inventory what
      step-level detail actually exists)
- [ ] Screenshots, videos, HAR, and other attachments
- [ ] Execution logs (`execution.log`, `kre-*.log` or equivalents)
- [ ] Unicode case names, long error messages, larger log volumes
- [ ] One suite-scale example (dozens–hundreds of cases) for
      performance measurement

Sanitisation requirements: no credentials, licence codes, customer
data, internal hostnames/IPs, or secrets in any artifact.

On receipt: inventory what the bundle actually contains, run the
side-by-side below, and record absent detail (e.g. no step data) as a
limitation — empty steps are not demonstrated parity.

## Acceptance gates (before promoting reporting-labs)

1. **Parity** — same canonical execution → both reports: outcomes,
   evidence attribution, timings, metadata, navigation compared
   side-by-side on the real corpus.
2. **Unresolved/distinct-counts** — genuinely skipped vs unresolved
   remain distinguishable in portal + report summaries.
3. **Access** — historical Allure reports remain viewable; project
   scoping, auth redirects, and traversal protections hold.
4. **Measurements** — generation time, single-report size, installed
   footprint, and actual Windows/Linux package sizes compared; audit
   whether anything besides Allure needs bundled Node before removal.
5. **Branding** — the generated report must display the approved
   IT-Space logo (same asset as the Allure report,
   `static/branding/iT-space-logo.png`), embedded inside the report
   itself (reporting-labs `logo` option → data URI) so downloaded /
   offline viewing keeps it with no external image dependency.
   Verified in authenticated portal viewing and downloaded reports,
   survives regeneration, does not interfere with navigation or
   security controls. Implemented in `adapters/reporting_labs.py`
   `_options()`; regression coverage in
   `test_it_space_logo_embedded_offline`; demo screenshot
   `02-rlabs-report.png` shows the rendered brand tile.
6. **Only then**: promote `reporting-labs` to `requirements.txt`,
   update packaging/installers, consider default switch, remove
   `3rdparty/allure3` + `3rdparty/nodejs` if unneeded.

## Provisional measurements (fixture scale only — NOT corpus numbers)

- Generation: ~30 ms per execution on the 4–7-case test fixtures.
- Candidate payload removal: `3rdparty/allure3` (~58 MB) + bundled
  Node (~230 MB) — potential, not measured in packages.
- reporting-labs package + generated embedded evidence still occupy
  space; single-file `index.html` ~300–700 KB in fixtures.
