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
Firefox (headless), Edge (=Edge Chromium — all platforms, Linux
drivers exist per Katalon 8.x notes), Edge (legacy, Windows-only),
Safari (macOS-only). Validation normalizes ids + legacy display
strings, rejects unknown values at launch as config errors, and
checks runner `os_arch` only for local-browser selections. When the
runner OS is unknown the choice is accepted and surfaced (not
guaranteed) — platform compatibility never implies the browser is
installed on the host.

## 5. Capacity / dispatch guarantees

- Atomic `QUEUED|DISPATCHED → PREPARING` claim — one winner; losers
  skip (`dispatch_skipped` audit) or hit lock/busy → retried next
  tick, session stays usable (fixture-verified, 2- and 3-process).
- Enabled/draining runners and `max(0, min(slots, license_slots) −
  active)` capacity enforced in `pick_runner`/`effective_slots`.
- Uncertain work is NOT auto-reexecuted: post-commit failure keeps a
  single execution; `reconcile_stale` marks ambiguous work UNKNOWN,
  never blind-redispatches.
- Cancellation boundary: pending (queued/dispatched) → atomic cancel
  wins or loses to the PREPARING claim; running → CANCEL_REQUESTED
  (**running-process kill unverified** — flag only).
- Timeout field: **missing** (no per-execution timeout config/arg).
- Artifact-return failure / stale-worker / restart-mid-run: states
  UNKNOWN/audited; no duplicate dispatch (impl; restart path covered
  by contention tests, host path pending).

## 6. Parallelism — stated precisely

1. **Independent executions on multiple workers** — supported via
   per-runner slots (fixture-verified claim races).
2. **Parallel suites inside a KRE collection** — depends on KRE
   suite-collection semantics; not a documented/verified path.
3. **Case-level sharding** — out of scope, not implemented.
Slots ≠ collection parallelism ≠ sharding.

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

1. Running-process termination unverified (kill path).
2. Desired-capabilities editing for remote browsers (impl gap).
3. Execution timeout field (impl gap).
4. Two-host/licensed-KRE evidence (access blocker).
5. Collection-parallelism semantics (unspecified).
