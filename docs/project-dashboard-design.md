# Project dashboard — design + data mapping (REVIEW, not built)

## Layout
Project page tab "Dashboard" — distinct from system health + setup:

```
┌ Filters: [period] [source ref] [suite] [environment] [status] ┐
┌ Cards: Executions N (ok/fail/err) | Test cases passed/failed/  │
│   error/skipped | Pass-rate x/y | Flaky (evidence only)        │
├ Trend: outcome-per-day · duration-per-day (comparable runs)    │
├ Recent executions: id·worker·engine·sha·duration·result·report│
├ Failures: test case → linked logs/screenshots/artifacts        │
├ Empty states: no-data / missing-report / collection-failed     │
```

## Metric → model mapping

| Card/metric | Source field | Notes |
|---|---|---|
| Executions (counts by state/outcome) | `Execution.state`, `Execution.outcome` | attempt-level counts |
| Test-case outcomes | `TestResult` rows (junit normalize) | **distinct from execution counts** |
| Pass-rate | passed_cases / (passed+failed+error) — skips excluded from denominator | define + show denominator |
| Duration trend | `Execution.started_at`/`finished_at` | only COMPLETED runs |
| Worker/engine/rev | `Execution.runtime_id`→RuntimeConnection, `Execution.dagu_run`, `GitBinding.last_synced_sha` | per-row links |
| Report availability | `Execution.report_status` (ready/partial/missing) | links to /report + zip |
| Failures | `TestResult.status=failed|error` + `Artifact` rows | links to evidence |

## Aggregation rules (before implementation)

- **Test identity:** (project, suite, case_ref, parameter-set-hash) — parameterized variants counted separately.
- **Retries/reruns:** each is a separate ATTEMPT (counted in executions); a test case is counted once per attempt, never summed across retries as new tests.
- **Errors/skips/cancel/timeouts:** separate buckets; never folded into pass-rate numerator or denominator silently.
- **Comparable-for-trend:** same suite + same environment + terminal state.
- **Missing results:** explicit "no parsed results" — never shown as zero-test pass.

## Missing data / proposed additions
- Per-case parameter-set identifier (parse junit `classname+name+params`).
- Environment tag on Execution (run_config.environment).
- Failure classification taxonomy (infra-vs-product signal).

## Deferred (evidence-gated, not labeled until supported)
Flaky detection (needs multi-run history + evidence, never "passed-on-retry" alone), new/recurring classification, requirements coverage, defect summary, release sign-off (must be explicitly authored — never inferred from pass rate).

## Initial scope vs later
Scope: filters, summary cards, trends, recent executions, failure links, empty states, printable project summary (HTML). Later: PDF export (needs a renderer — decision deferred; start with print-styled HTML).
