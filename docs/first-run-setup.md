# Guided first-run setup v2 — corrected design (REVIEW, not built)

## Required-component mapping

| Step | Required component | Installer-provided | User input | Verification | Failure/recovery |
|---|---|---|---|---|---|
| 1 | Admin credential | bootstrap Admin | new password (≥12) | strength + confirm | retry inline; step blocks |
| 2 | Dagu coordinator | config, ports, worker token (written at install) | none | coordinator health endpoint + worker listener (checked server-side) | retry button + journal hints; "not configured" vs "configured-failed" shown distinctly |
| 3 | ≥1 Worker | local worker offered as default | remote: copies coordinator URL + label to provision | worker visible via /workers AND label registered | if absent after timeout: shows exact worker-side command to run; resume keeps step state |
| 4 | ≥1 KRE execution engine | none (external licensed) | executable path + target worker | presence+version probe ON THAT WORKER | unverified blocks; probe failure shows worker/path it failed on. License readineess NOT claimed — only presence/version verified |
| 5 | ≥1 source binding | none | repo URL + ref + auth method (credential fields conditional on method) | clone + ref resolve via same dispatch path a worker would use (not just save) | failure names URL/ref/auth stage; edit-and-retest inline |
| 6 | Report/evidence publishing | local artifact store configured at install | publishing repository address + credentials (conditional on publish mode: local-only vs git-push) | repo reachable + WRITE permission probe (test push or permission API) for git mode; dir writable+free-space for local | publish-repo failure blocks only remote-publishing feature, flagged separately in readiness |
| 7 | Security/bind | loopback default | bind address + TLS cert (conditional on external exposure) | handshake+address reachable | invalid cert blocked; loopback dev allowed |

## Verification — configured ≠ verified (per component)
Each check states: how installed → what's checked → where it runs →
success criterion → failure display. Auto-configured items still run
their check (Dagu coordinator is auto-*configured*, verified by health
poll, not assumed ready).

## Execution readiness is per-worker
Setup requires: ≥1 worker connected AND ≥1 KRE verified ON AN ELIGIBLE
worker for the chosen binding/runtime combination — not merely "a
worker exists somewhere" + "a KRE exists somewhere". Submission-time
re-validates prerequisites (409 naming the missing piece). Worker
disconnect at runtime = operational failure state; setup progress is
persisted and unchanged.

## Source + storage definitions
- "Binding resolved" = clone+ref resolution succeeds through the real
  execution path (Dulwich/cli backend the worker will use), not a saved
  URL.
- Storage checks: instance dir writable, DB migrated, report/evidence
  destination writable + sized. Repo access ≠ publish permission — a
  write probe is required before "publishing verified".

## Seven steps
1. Welcome + Admin credential
2. Orchestration (auto-config, verified health)
3. Worker (local default / remote instructions, verify registration)
4. Execution engine — KRE presence/version probe on selected worker
5. Source binding — URL/ref/auth, real-path resolve test
6. Reporting & publishing — storage health + publish-repo write probe
7. Security — bind address + conditional TLS
8. Readiness summary — per-component verified/configured/missing;
   Finish enabled only when all blocking checks verified; each row
   links to its Settings page for repair.
Resume: `setup_state` per-step persisted; re-entry resumes at first
incomplete blocking step.

## Shared configuration
Setup renders the SAME forms/validators as Settings (network,
infrastructure/workers, secrets, reporting) — no parallel system.
Settings → "Setup & diagnostics" re-opens the flow anytime.

## Conflicts flagged
- Existing backend allows publish_disabled → new requirement makes
  publishing REQUIRED: publish repo config+write check becomes a
  blocking step unless explicitly local-only mode is chosen AND
  accepted as a supported deployment (decision needed: is a
  local-artifact-only deployment a valid full system, or must
  publishing always have a repo? Currently designed as blocking
  conditional on mode — flag for your call).
- Licensed KRE execution cannot be verified without a license —
  setup verifies presence/version only; a licensed smoke test is a
  separately-approved operational check, not part of setup.
