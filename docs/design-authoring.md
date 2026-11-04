# Native authoring design (revised — pending approval)

## Permissions — separate gates
- `git_bindings.report_writable` — report publication/deletion only.
- `git_bindings.authoring_writable` — native suite/collection editing
  and publication. One does not imply the other; a binding can be
  read-only, report-writable, authoring-writable, or both.

## Artifact contract (must be verified, not assumed)
- Suite: the bound project's actual `*.ts` `TestSuiteEntity` layout
  (member list, GUIDs, hooks, settings) — serializer built from the
  repository's own file, not from generic XML names.
- Collection: `TestSuiteCollectionEntity` +
  `TestSuiteRunConfiguration` blocks (suite ref, profile, browser,
  retry) from the repository's real collections.
- Dynamic suites: **read-only** for this phase — query membership is
  not editable; shown with an explicit unsupported-state reason.

## Preservation guarantee
Round-trip via lxml: only the member list + whitelisted fields are
replaced; all other nodes/attributes/comments/namespaces pass through
unmodified. If the file structure isn't confidently supported (odd
namespaces, unexpected roots, companion-file dependencies), the
editor is read-only with the reason shown — never a destructive
rewrite.

## Draft lifecycle + coordination
- `authoring_worktree` per binding (separate from fetch + report-pub).
- Per-user draft = edit session; a binding-level lock (the existing
  `reports-pub` flock pattern) serializes concurrent edits on the
  same binding. Multi-user drafts are per-session — an already-open
  edit blocks a second until saved or abandoned.
- Base revision recorded at draft open; publish compares base vs
  upstream head — upstream same-path change = conflict, blocked until
  resolved, never force-push.

## Publish flow
Edit -> draft diff preview (repo, destination branch, base SHA,
changed paths, diff) -> approve -> push. Approval is bound to the
exact diff + destination; any further change requires renewed review.
Write access requires `authoring_writable` + suitable credentials —
report read/write does not grant it.

## Lifecycle
Drafts: open -> edit -> save (draft) -> publish or abandon. Audit
events at each transition. Cleanup: abandoned drafts expire;
published drafts record the destination SHA.
