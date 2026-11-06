# Acceptance freeze — Dagu/agent fixture-chain milestone (2026-11-03)

Criteria FROZEN at commit `49e76de`. No further status-only iteration on
this milestone. Regression tests below are retained and must keep passing.

## Frozen acceptance criteria (all met at fixture level)

1. `flask kre-smoke` is probe-only (`<exe> -version`); it cannot start
   project tests. `flask kre-exec` requires an explicitly selected
   `--suite`.
2. The Dagu workflow is the mandatory orchestrator (Option B). The step
   `agent --once --monitor` executes locally assigned tasks and otherwise
   monitors the assigned execution to a terminal outcome or explicit
   uncertainty — never finishing after dispatch.
3. `flask worker` reconciles: expired leases → UNKNOWN (never re-run);
   finished Dagu runs without portal result → UNKNOWN; running Dagu runs
   never extend execution leases.
4. Secrets: stored via Fernet SecretRef only; agent tokens digest-only,
   shown once; the runtime invocation credential reaches argv as
   `-apiKey=…` but is redacted from `kre-invocation.log` and scrubbed
   from captured stdout/stderr before evidence collection.
5. Dagu auth: `auth.mode: basic` (verified vs bundled 2.25.8) or a
   builtin-mode Bearer API key.
6. A failed test assertion produces `state=completed` +
   `outcome=failed` with full evidence — never an infra failure, never a
   fabricated pass.

## Retained regression tests

- `tests/test_runbook_wiring.py` — probe/exec semantics, dagu-config,
  token issuance, state-endpoint scoping, reconcile wiring, secret scrub
  across logs/evidence/UI/audit/Git, runbook fixture path e2e.
- `tests/contract/test_dagu_provisioning.py` — provision → boot bundled
  Dagu 2.25.8 → auth enforcement → workflow presence (no mocks).
- `tests/contract/test_dagu_contract.py` — submit/status/CLI-stop.
- `tests/test_pipeline.py`, `tests/test_agent.py`,
  `tests/test_dagu_reconcile.py` — pipeline, agent transport, UNKNOWN
  semantics.
- Full suite: **125 tests**.

## Open acceptance gates (explicitly NOT part of this milestone)

| # | Gate | Owner of the input |
|---|---|---|
| 1 | Real-KRE A01 against an operator-managed installation | operator KRE binary + licence |
| 2 | Live ReportPortal publication with correctly associated screenshots | live RP instance |
| 3 | Execution + cancellation on a genuinely separate runner host | second host |
| 4 | Complete self-contained installer + service lifecycle | installer milestone |
| 5 | Supported-platform install / reboot / update / recovery | target hosts |

Evidence: `docs/dagu-integration-evidence.md` (live fixture runs —
remote success, disconnect→UNKNOWN, local mode, cancel demo),
`docs/kre-operator-runbook.md` (executable runbook for real KRE).
