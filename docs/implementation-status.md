# Implementation status inventory

Date: 2026-11-03. Required by handoff §3. Latest implementation commit:
`1c88f1e` (+ follow-up commit on `main`).

Columns per request: **implementation** (code completeness),
**automated tests** (suite evidence), **live integration** (against real
services), **external blockers**. "Implemented" here means code exists and
is exercised — never a claim of live verification (A14).

## Execution call path (clarification, item 2)

Current actual path for a dispatched execution:

```
suite run -> create_execution (immutable snapshot + outbox row, same txn)
  -> flask worker -> pick_runner (capacity/drain/project scope)
  -> local: KRE adapter subprocess -> collect -> normalize -> publish -> finalize
  -> remote: execution stays DISPATCHED under lease; the authenticated
     agent polls /api/agent/tasks, invokes the operator-installed KRE on
     its own machine, heartbeats, uploads results+evidence
```

Dagu is the OWNER-APPROVED mandatory orchestrator (Option B, 2026-11-03):
the outbox submits to Dagu first and persists the workflow-run id before
the worker executes under that correlation; with Dagu unreachable and
mandatory mode on, dispatch fails honestly — no self-orchestration, no
blind resubmission. The Dagu client is still unverified against the
pinned v2.25.2 API — marked so, not claimed integrated.

## Status table

| Capability | Implementation | Automated tests | Live integration | External blockers |
|---|---|---|---|---|
| About/branding (v1.7) | complete | test_about.py AB01–AB07 (29 tests) | n/a (all local) | official logo (owner input) |
| Bootstrap/network/certs (v1.8) | complete | test_bootstrap/test_network/test_certs (BOOT01–04, NET01–05, CERT01–04) | n/a | none |
| Execution pipeline: snapshot, outbox, state machine, local runner, cancel, reconcile | complete | test_pipeline.py (6 tests incl. cancel-tree kill, lease→UNKNOWN) | none — local only | none |
| Remote runner transport | complete | test_agent.py (5 tests: auth, tasks, heartbeat, artifact, cancel, reconcile) | unverified vs. a real remote host | none |
| KRE adapter | complete for invocation mechanics | test_pipeline.py via fixture executable | **unverified vs. real KRE** | operator KRE binary + licence |
| Dagu orchestrator (OWNER-APPROVED Option B, mandatory) | dispatch submits to Dagu first, dispatch_id persisted, fail-closed when unreachable; `--monitor` step tracks remote runs to terminal/unknown; reconcile wired into worker | contract + wiring tests (submit/auth/reconcile/monitor scoping) | **verified vs bundled 2.25.2** — basic auth, remote/local fixture runs incl. disconnect→UNKNOWN (docs/dagu-integration-evidence.md) | none at fixture level |
| ReportPortal adapter | client + resume-safe ID persistence | unit shape only | **unverified vs. real RP** | live RP instance |
| Git persistence | export + push + conflict + restore | test_infra.py (local repos); push/conflict code paths | unverified vs. remote remote | real remote repo + credentials |
| Credential store | complete | test_infra.py (4 tests) | n/a | none |
| Users/roles/audit | complete | test_infra.py, test_security.py | n/a | none |
| Portal UI (projects→executions) | complete | test_infra.py scope tests | n/a | none |
| Suite/plan/release builders | partial — create + run + mark-released; no rich builder UI | test_infra.py | n/a | none |
| Installer | scaffold `scripts/install.py` | dry-run only | **missing per-OS validation** | Windows/SLES test hosts |
| Updater | signed-bundle verify + backup/stage/rollback scaffold | none | **missing live update test** | signed release bundle + target hosts |
| PQ transport check | documented pending | none | missing | PQ-capable client matrix |

## Reclassified (item 6)

Remote transport and installer/updater are **missing implementations**,
not operator-blocked validation — now coded (agent + scaffolds) with live
verification still open. Operator-blocked items are only: real KRE
binary/licence, live Dagu, live ReportPortal, owner-approved logo, PQ
client matrix.

## Acceptance evidence (item 10)

- **A01/A02**: blocked on real KRE + ReportPortal (operator inputs).
- **A03**: `test_pipeline` — RP/Git failures leave evidence + state fields.
- **A04**: partial — multiple runners supported; second-runtime live run unverified.
- **A05/A06**: `test_pipeline.py::test_cancel_kills_process_tree`,
  `test_lease_expiry_reconciles_unknown_not_rerun`.
- **A07**: snapshot SHA pinned per binding; multi-binding resolution partial.
- **A08**: `restore_definitions` implemented; full-clone restore tested on local repos.
- **A09/A10**: scaffolded; per-OS validation open.
- **A11**: roles + project scope enforced server-side; matrix partially tested.
- **A12**: test_bootstrap.py + reset/self-service tests.
- **A13**: cert import/network rollback tested; PQ explicitly open.
- **A14**: unverified integrations labelled here and in code/docs.
