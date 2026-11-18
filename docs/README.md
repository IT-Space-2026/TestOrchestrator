# docs/ — developer and release documentation

This directory holds **development-side** documentation only.
User/administrator documentation lives in `../Manuals-Doc/` (Admin
Manual, User Manual, Screenshot-Index) — consult those first; this
tree is for people building or releasing TestOrchestrator.

## Contents

| File | Class | Content |
|---|---|---|
| `build.md` | developer | One-command build pipeline, prerequisites, GPL-approval flag, output layout, validation gates |
| `licensing.md` | developer | `TO1.` licence format, payload fields, Ed25519 verification, enforcement points |
| `integration-contracts.md` | developer | Version pins + endpoint shapes for external integrations (referenced from `testorchestrator/dispatch.py`) |
| `security-review.md` | evidence | First-party security review findings + dispositions |
| `report-serving-security.md` | developer | Report-serving trust boundary — including the same-origin limitation that is NOT a sandbox boundary |
| `redistribution-review.md` | release record | GPL redistribution analysis: separate-process boundary, corresponding sources, written-offer terms |
| `acceptance-2.5.0.md` | release record | 2.5.0 acceptance matrix + results on the tested build |
| `incidents/2026-11-04-stale-frozen-payload.md` | incident record | Closed packaging-defect postmortem |

## Note for the initial release

Pre-release development documents (superseded design spikes, decision
records, per-version release records) were removed for the 2.5.0
initial public release; git history retains them.
