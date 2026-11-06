# Dashboard unification — Plotly architecture + migration checklist

## Inventory (complete)

| Page | Chart | Current impl | Library |
|---|---|---|---|
| Main dashboard | "Outcomes per day · 14d" | CSS `div.bar-col` columns + title tooltip | none (hand-rolled) |
| Main dashboard | stat cards, project table, recent executions | HTML | — (kept as tables/cards) |
| System health | none | tables | — |
| Executions | none | tables | — |

Only ONE existing chart exists — the hand-rolled CSS bar chart.
Migration target: replace it with a Plotly figure; keep tables/cards.

## Agreed architecture
- `plotly.py` (Plotly Express first, Graph Objects for fine control)
  for Python figure construction; JSON embedded in the page.
- Locally bundled `plotly.js` (pinned, vendored in
  `static/vendor/plotly.min.js`) — loaded once per page.
- Portal templates render; aggregation stays in Python — figures are
  presentation only.
- NO Dash, no runtime CDN, no external chart service.

## Design system (all dashboards)
- Palette: pass=green, fail=red, error=orange, skip=gray, running=blue.
- Shared `chart_theme.py` (fonts, sizing, tooltip, date format).
- States: loading, empty, partial-data, error — each chart renders a
  labeled state, never a blank canvas.
- Every chart has an underlying data-table fallback + drill-down link.

## Metric correctness (unchanged unless documented)
- Pass-rate = passed_cases / (passed+failed+error) — skips excluded.
- Executions counted per attempt; test cases per attempt-run — never
  summed across retries.
- Comparable-for-trend: same suite + environment + terminal state.

## Migration checklist
1. Vendor plotly.js pinned + add `plotly` to requirements (+wheels).
2. `chart_theme.py` shared theme module.
3. Rebuild "Outcomes per day" as Plotly figure (same data source).
4. Project dashboard charts (outcome/pass-rate/duration trends,
   failure breakdown) per project-dashboard-design.md.
5. Remove CSS bar-chart classes + any unused assets.
6. Screenshots before/after every affected page; labeled fixtures
   (normal/empty/failed/partial/large).
7. Full suite + visual review.
