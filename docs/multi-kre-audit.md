# Multi-KRE / plan capability audit (commit 702af12+, licence fix pending push)

KRE model (documented): `katalonc` is a CLI engine the worker launches
and supervises — there is no remotely controllable KRE server/API.
Dispatch, cancellation, and reconciliation are the Orchestrator's own
responsibility. Floating licences are per concurrent SESSION.

Readiness levels kept distinct (never conflated):
worker reachable → KRE executable probeable → project/env prereqs →
licence/auth configured → approved execution demonstrated. A version
probe is never labelled a licensed execution.

## Capability matrix

| Capability | Status | Source / UI | Tests & evidence | Gaps |
|---|---|---|---|---|
| Multiple registered workers | DONE | `Runner` model, Admin→Infrastructure | route+model tests | live 2nd host = GATE |
| KRE exec/version/config per worker | DONE | `RuntimeConnection` (path, detected_version, last_diagnostic), probe route | probe tests | real KRE probe = GATE |
| Configurable concurrent slots | DONE | `Runner.max_slots`, `RuntimeConnection.concurrency` | dispatch tests | — |
| Licence-aware capacity | DONE (new) | `Runner.license_slots` + `capacity_limit()` = min(slots, seats); unset ≠ assumed seats | test_capacity_limit_licence_aware | seat discovery never claimed |
| Project/suite/collection/profile/browser selection | PARTIAL | suite/suite-entry/plan models; KRE args projectPath/testSuitePath/executionProfile/browserType | adapter arg tests | suiteCollectionPath flag exists in build_args set; plan→collection mapping needs real-KRE validation = GATE |
| Plan create/edit/execute + scheduling | PARTIAL | `Plan` model + run; no schedule CRUD UI | plan tests | schedule mgmt ABSENT (Dagu cron possible, no UI) |
| Sequential dispatch | DONE | outbox → `flask worker` → pick_runner | pipeline tests | — |
| Parallel dispatch | PARTIAL | capacity loop picks per-execution runners | slot tests | multi-worker parallel evidence = GATE |
| Queueing & capacity limits | DONE | queued state + effective_slots lease | pipeline tests | — |
| Isolated checkouts/workspaces/report dirs | DONE | `prepare_workspace` at recorded SHA + per-run report_dir | pipeline tests | — |
| Timeouts/cancellation/process cleanup | DONE | run_invocation timeout + `_kill_tree` process-group kill | cancel tests | — |
| Disconnect/reconnect + reconciliation | PARTIAL | lease→UNKNOWN reconcile, dagu monitor | reconcile tests | live disconnect = GATE |
| Duplicate-dispatch prevention | DONE | submit-first correlation + durable outbox | dagu tests | — |
| Plan/suite/attempt-level results | PARTIAL | per-execution results+artifacts; KRE retry flags recorded as extra args | results tests | attempt-level persistence of KRE retries = work needed |

## Plan semantics (explicit — not a Katalon suite collection)

An Orchestrator Plan = ordered set of suite entries with dispatch
policy: sequential or bounded-parallel across workers, per-entry
failure policy (stop-plan vs continue), single cancel scope, no
automatic retry (retries live in KRE config only — never multiplied),
aggregate outcome = worst of entries (failed > broken > passed;
any-failed ⇒ plan failed). Worker selection = project scope +
capacity + non-draining. A plan is NOT assumed equivalent to a Katalon
suite collection or TestOps object.

## Licence/concurrency rules implemented

- `capacity_limit` = min(max_slots, license_slots); licence seats are
  operator-declared, never probed/invented.
- Nested concurrency: Orchestrator limits concurrent KRE processes;
  `-testSuiteCollectionPath` parallel mode inside KRE is out of our
  dispatch control — flagged for version-gated validation.
- `-enableLicenseRetry`/interval/duration: pass-through `extra` args
  today; version-gate pending real-KRE evidence. Licence-wait ≠
  executing.
- KRE `retry`/`retryStrategy` = KRE-side; dispatch never auto-retries
  ambiguous completions (AMBIGUOUS blocks retry).

## Credentials/networking

- `-apiKey` support via `extra` + `redact` list: scrubbed from recorded
  argv, stdout/stderr evidence (`_scrub_file`). Process-argv exposure
  documented — no invented env-var replacement.
- Licence-vs-test proxy settings, offline licence under service
  account `.katalon/license`, KRE's own JDK-21 requirement (separate
  from Allure's Java) — all documented prerequisites; service-account
  licence validation = GATE.

## Deferred evaluations (positions, not claims)

- **PyInstaller one-dir:** correct direction for focused packaging —
  pending spike: waitress+flask+sqlalchemy+alembic+wtforms+argon2/cffi
  hidden-import coverage; install.sh must invoke the frozen binary.
- **Dulwich backend:** repo-backend seam needed behind
  `git_runtime`/`gitsync` call sites (clone/fetch/checkout-sha/
  push/export); LFS/submodule/SSH/proxy/custom-CA matrix unverified —
  no silent system-git fallback. Superseded components removed only
  after the backend passes acceptance.
