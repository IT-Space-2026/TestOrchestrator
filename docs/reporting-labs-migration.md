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
5. **Only then**: promote `reporting-labs` to `requirements.txt`,
   update packaging/installers, consider default switch, remove
   `3rdparty/allure3` + `3rdparty/nodejs` if unneeded.

## Provisional measurements (fixture scale only — NOT corpus numbers)

- Generation: ~30 ms per execution on the 4–7-case test fixtures.
- Candidate payload removal: `3rdparty/allure3` (~58 MB) + bundled
  Node (~230 MB) — potential, not measured in packages.
- reporting-labs package + generated embedded evidence still occupy
  space; single-file `index.html` ~300–700 KB in fixtures.
