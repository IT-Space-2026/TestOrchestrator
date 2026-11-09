# Main dashboard — TestOps-inspired layout (PROPOSAL)

The agreed direction is Katalon TestOps–inspired; the detailed
reference is not in the repo, so this is a proposal, not parity
claim. Orchestrator branding retained; no new metrics/services.

## Layout

```
┌ Header: "Dashboard" · project-scope badge · period selector [7/30/90d] ┐
├ KPI cards (4): Executions | Passed cases | Failed cases | Flaky*       │
│   *only if evidence — else hidden, never a zero                         │
├ Trend row (2 charts): Outcome per day · Duration per day               │
├ Two-col row:                                                          │
│   Left  Top failing cases (linked)      │  Right  Recent executions   │
│   (case · failures · latest exec link)  │  (id·worker·engine·sha·    │
│                                          result·duration·report link)│
├ Projects strip: per-project rollup cards (name · last result · execs) │
└ Empty states: "No executions in period" / "No results yet"             │
```

## Data → source

| Element | Source | Status |
|---|---|---|
| Executions KPI | `Execution.state/outcome` counts | impl |
| Pass/fail cases | `TestResult` status | impl |
| Outcome/duration trend | `charts.py` figures | impl |
| Top failing | `TestResult` failure ranking | impl |
| Recent executions | `Execution` rows + worker/engine/sha | impl (cols added) |
| Projects rollup | per-project `dashboard.snapshot` | impl |
| Period filter | `?days=` query → `dashboard.snapshot` | proposed |
| Flaky | needs multi-run evidence | deferred (evidence-gated) |

## Distinction from Project Dashboard
Main = cross-project overview + drill-down to project dashboards.
Project = per-project detail (already matches design doc).
