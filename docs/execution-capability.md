# Execution capability — implementation vs verification

Status: unreleased source. Evidence classes: **impl** (code exists),
**fixture** (fixture KRE verified), **host** (needs licensed env).

## 1. Execution-path audit matrix

| Setting | Manual run | Schedule | Plan | Snapshot | KRE arg | Evidence |
|---|---|---|---|---|---|---|
| Repository/revision | binding + resolved sha | same via schedule | same | `source_sha` + snapshot | `-projectPath` | fixture |
| Suite / collection | suite id | suite or plan target | entries | `suite_name`,`suite_version` | `-testSuitePath` | fixture |
| Execution profile | `execution_profile` field | same | inherits schedule | `run_config.profile` | `-executionProfile=` | fixture |
| Browser (local KRE host) | selector | selector | inherits schedule | `run_config.browser` | `-browserType=` | fixture (Firefox end-to-end) |
| Remote WebDriver | URL + driver-type fields | same | inherits schedule | `run_config.remote_webdriver_*` | `-browserType=Remote` + `-remoteWebDriverUrl=` + `-remoteWebDriverType=` | fixture (args form only) |
| Desired capabilities on grid | — | — | — | — | **missing** — comes from KRE project desired-caps, not these fields | impl gap |
| Runner/runtime pick | `pick_runner` + runtime | same | same | `runner_id`, `runtime_id` | executable path | fixture |
| Timeout | — | — | — | — | **missing** (no timeout field/arg) | impl gap |
| Retry (KRE) | `kre_options.retry` | via run_config | inherits | `run_config.kre_options` | `-retry` (version-gated) | fixture (gating) |
| Orchestrator rerun/retry | retry action, rerun-failed | — | plan rerun | `attempt`,`retry_of` | new execution | fixture |
| Version-gated options | `kre_options` map | same | same | whitelisted | allowlisted flags only | fixture |

## 2. Precedence (deterministic)

`launch form | schedule.run_config | plan schedule's run_config` →
**immutable execution snapshot** → adapter defaults only when unset
(`Chrome (headless)` documented fallback, never overwrites explicit
choice). Snapshot is written at `create_execution`; later edits to
schedules/plans/defaults never mutate a stored execution. Resolved
config is shown on the execution page; credential userinfo in remote
URLs is scrubbed from logs/audit/artifacts.

## 3. Remote WebDriver contract (corrected)

Katalon-documented form: `-browserType=Remote` (required) +
`-remoteWebDriverUrl=<endpoint>` + `-remoteWebDriverType=Selenium`
(typical). Earlier code dropped `-browserType` — corrected. The
URL/type alone does **not** pick a browser on the grid — the node's
browser comes from the KRE project's desired capabilities
(impl gap: no Orchestrator-side capability editor). Local KRE-host OS
restrictions are NOT applied to the remote node's browser.

## 4. Browser compatibility

`SUPPORTED_BROWSERS`: Chrome, Chrome (headless), Firefox,
Firefox (headless), Edge (=Edge Chromium — argument accepted on all
platforms; actual runtime support requires the browser installed on
the KRE host — **not guaranteed, unverified per KRE version**),
Edge (legacy, Windows-only), Safari (macOS-only). Validation normalizes ids + legacy display
strings, rejects unknown values at launch as config errors, and
checks runner `os_arch` only for local-browser selections. When the
runner OS is unknown the choice is accepted and surfaced (not
guaranteed) — platform compatibility never implies the browser is
installed on the host.

## 5. Capacity / dispatch — per-item evidence matrix

| Item | Evidence |
|---|---|
| One job, competing workers | `test_schedule_contention_independent_processes` (N=2,3) — 1 execution |
| One-slot capacity, competing jobs | `test_dispatch_then_cancel_still_cancels_queued_scope` + effective_slots unit coverage — queued when full; NOT proven under two live jobs racing one slot — **partial** |
| Licence allowance | `effective_slots` caps at `min(slots, license_slots)` (impl); exhausted-licence race **unverified** |
| Disabled/draining runner | `pick_runner` filters `enabled & not draining` (impl); **unverified** end-to-end |
| Stale worker | `reconcile_stale` → UNKNOWN (impl); host path **unverified** |
| Restart during execution | UNKNOWN/audited path (impl); mid-run restart **unverified** |
| Artifact-return failure | collection failure marks execution, no re-dispatch (impl); **unverified** |
| Running-process cancel | `test_cancel_kills_process_tree` — killpg TERM→KILL, parent+child dead, state CANCELED, evidence preserved, slot held until confirmed stop (**fixture-verified; real-KRE kill unverified**) |
| Pending cancel | `test_cancel_between_claim_and_launch_aborts` (synchronized) |
| Collection parallelism | **unverified** — see §6 |

The atomic claim prevents two workers taking one job; it does NOT by
itself prevent two jobs exceeding one runner's slots — capacity is a
separate `effective_slots` check, partial coverage noted above.

## 6. Parallelism — stated precisely

1. **Independent executions on multiple workers** — supported via
   per-runner slots (fixture-verified claim races).
2. **Parallel suites inside a KRE collection** — depends on KRE
   suite-collection semantics; not a documented/verified path.
3. **Case-level sharding** — out of scope, not implemented.
Slots ≠ collection parallelism ≠ sharding.

**Collection semantics (inspected):** the Orchestrator passes the whole
suite/collection to KRE via `-testSuitePath=` — any intra-collection
parallelism is KRE-side config (suite collection execution settings),
invisible to the Orchestrator. A global `-browserType=`/`-executionProfile=`
arg applies KRE-wide — it does not rewrite collection-level browser/
profile entries; those resolve inside KRE. One collection execution
consumes one Orchestrator slot + licence allowance; the Orchestrator
sees only the invocation's aggregate results, not internal sessions.
Desired-capabilities editor deferred — the supported path is remote
URL+driver-type here, capabilities in the KRE project settings.

## 7. Retry layering

Orchestrator retry (new execution, `attempt`/`retry_of` lineage,
separate artifacts) and KRE `-retry` (inside one invocation,
version-gated, reject-by-default) are distinct layers — documented so
they don't multiply silently.

## 8. Two-host acceptance runbook (needs authorized hosts)

Central app → real repo/rev → manual+scheduled submission →
second-host worker/runtime → chosen browser/profile → licensed KRE
run → results/logs/screenshots/report → dashboard+audit update.
Cases: 1 pass, 1 intentional failure, 1 capacity-queueing, 1 cancel
(kill-path evidence). Prerequisites: licensed KRE + second host +
browsers + (Java/Allure optional) + Azure DevOps creds if used.
**Blocker: no authorized second host / licensed KRE here.**
See `docs/runbook-authorized-host.md` for the base procedure.

## 9. Remaining gaps (ranked)

1. Real-KRE running-process termination (fixture-verified kill path;
   licensed-KRE kill unverified).
2. Desired-capabilities editing for remote browsers (deferred —
   supported path: URL+type here, capabilities in the KRE project).
3. Two-host/licensed-KRE + exhausted-licence evidence (access).
4. Collection-parallelism semantics (KRE-side, not observable).
5. Timeout semantics: `timeout_seconds` — running-time from process
   launch, enforced by the same killpg path, persists in snapshot,
   shown in detail; empty = 3600 s default (fixture-verified).
