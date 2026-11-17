# docs/ — developer and release documentation

This directory holds **development-side** documentation only.
User/administrator documentation lives in `../Manuals-Doc/` (Admin
Manual, User Manual, Validation-Record, Screenshot-Index) — consult
those first; this tree is for people building or releasing
TestOrchestrator.

## Contents

| File | Class | Content |
|---|---|---|
| `build.md` | developer | One-command build pipeline, prerequisites, output layout, validation gates |
| `licensing.md` | developer | `TO1.` licence format, payload fields, Ed25519 verification, enforcement points |
| `integration-contracts.md` | developer | Version pins + endpoint shapes for external integrations (referenced from `testorchestrator/dispatch.py`) |
| `dagu-verified-api.md` | developer | Dagu API surface verified against the bundled 2.27.0 binary |
| `security-review.md` | evidence | First-party security review findings + dispositions |
| `report-serving-security.md` | developer | Report-serving trust boundary — including the same-origin limitation that is NOT a sandbox boundary |
| `release-notes-2.0.1.md` … `release-notes-2.0.3.md`, `release-summary-2.0.0.md` | release records | Per-version release documentation |
| `decisions/dagu-mandatory-backend.md` | decision record | Dagu is the mandatory orchestration backend (approved) |
| `incidents/2026-11-04-stale-frozen-payload.md` | incident record | Closed packaging-defect postmortem |

## Removed in the 2.0.3 docs audit

Completed plans, acceptance/handovers, proposals, dated status
snapshots, and audits superseded by the manuals and
`Manuals-Doc/Validation-Record.md` were deleted (33 files + `eval/`,
`evidence/`, `ui-screenshots/`); git history retains them. Content
that was still valid was merged into the Admin Manual — Windows
install detail into §5, the three-context private-repo auth model
into §19.
