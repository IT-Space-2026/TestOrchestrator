# Plan retirement transition (proposal — pending approval)

## Inventory (current usage)
- `plans` rows + `plan_entries` (suite_id ordered) + `parallelism`.
- Dependencies: `schedules` (target_type='plan'), `releases`
  (plan_id link), executions (plan_id snapshot), `run_plan`/
  `rerun_plan_failed` routes, plan detail page.
- Historical executions keep `plan_id` in their immutable snapshot —
  never rewritten.

## Transition — preserve vs use
- **Preserve:** all Plan rows, entries, schedule links, release links,
  and execution history stay readable — no destructive migration.
- **Deprecate (proposal):** hide "Create plan" for new work; Plans
  stay accessible at their detail URLs for existing schedules/runs.
- **Convert (optional, reviewed):** "Export plan as Katalon
  collection" — writes a `TestSuiteCollectionEntity` to the bound
  binding (requires `authoring_writable`); plan parallelism has NO
  collection equivalent → flagged as unconverted setting, never
  silently dropped.
- **Not a rename:** a Plan's app-side settings (parallelism,
  scheduling, routing) stay app-side; a collection is only the
  ordered suite list.

## Not silently discarded
- Plan parallelism, run-config overrides, schedule recurrences —
  app-only, cannot be represented in a `.ts` file.
