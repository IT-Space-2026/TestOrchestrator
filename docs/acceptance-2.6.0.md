# TestOrchestrator 2.6.0 — acceptance evidence ledger

**Branch**: `feature/2.6.0-node-runners` (PR #4 — draft).
**Current head at recording**: `d6c14b3` — logging implementation
(CSV export + structured output; application code). A ledger-only
commit follows it.
**Tested candidate**: `57445bc` (full-suite Run C below). Historical:
all commits between `57445bc` and `34c2cca`…`adf4490` were
documentation-only, so Run C remained the tested state until
`d6c14b3`. **`d6c14b3` changed application code** — a new full-suite
run is required on this head (Run E below) and the packaged-candidate
suite stays a required row in `docs/packaged-acceptance.md` §6.

**Stage 5 status: parallel + recovery scenarios exercised; PB-loss
retry/orphan correctness implemented and re-verified (`b049fab`,
pushed).** The earlier "same brid re-ran → PASSED" row proved only a
final outcome. The mechanism is now: per-attempt pidfile +
`TO_EXEC_ID` env marker (kills are env-verified), retry-prep orphan
resolution, per-attempt `attempt-<buildno>` artifact dirs, portal-side
`bb_attempts` snapshot tracking + `buildbot_retry_attempt` audit.
Re-verified live on both transports — see "Worker PB loss" below.
The original 11:12 run remains marked as the pre-fix evidence it is.

**Explicit policy chosen (per transport):**
- **SSH/agentless** → policy 1, reattach without relaunch: the remote
  `setsid` run (run.pid/pgid) persists across dispatcher death; the
  reconnecting dispatcher RESUMES it — true single-launch, verified
  (remote 3791749 resumed, new dispatcher 3791835).
- **PB workers** → policy 2, confirm-then-configured-retry: the retry
  `prep` step resolves the recorded pid — env-marker-verified kill if
  alive, "already exited" if not — before re-staging; the interrupted
  attempt is recorded (RETRY, own artifact dir, audit) and Buildbot's
  unconditional resubmit becomes the explicitly-permitted retry,
  each attempt a distinct identity+outcome.

**Retry boundaries (hardened, live-verified `a74884f2`):**
- *Confirmed termination, not just signal sent*: prep polls
  `/proc/<pid>` after TERM (3 s) then -9 (5 s); only when procfs is
  gone does the build proceed — "terminated orphaned prior attempt
  pid N (confirmed dead)". An unkillable/D-state process yields
  `TO_RETRY_BLOCKED` and the launch never happens.
- *Marker mismatch or uninspectable blocks relaunch*: a live pid
  without the execution's `TO_EXEC_ID` — including an environ that
  cannot be read — exits 70 with `TO_RETRY_BLOCKED`; the reconciler
  maps that marker to **UNKNOWN** (reservations retained, row lands
  on the reconciliation queue, `execution.retry_blocked` audited).
  The pidfile is kept as investigation evidence.
- *Retry enablement and limits are explicit*: `retry_enabled`
  (default true) and `max_attempts` (default 3, counting the
  original launch) come from `run_config`, rendered into every exec
  spec, enforced by a per-execution `.to_exec_attempt_<id>` counter
  in the builddir — attribution alone is not the policy, the counter
  is. Live: `a74884f2` retry printed `attempt 2 of max_attempts=3`,
  resolved pidfile 70011, relaunched as distinct pid 70177, PASSED;
  `bb_attempts` = [build 128 RETRY, build 129 SUCCESS].
- *Side-effect warning*: stopping an interrupted attempt cannot undo
  test actions already performed — a retry may repeat external side
  effects. Documented in Admin-Manual §18.1 with the
  `retry_enabled=false` escape hatch for non-idempotent suites.
- Regression coverage: `tests/test_retry_prep.py` (7 — real-process
  kill-confirm, foreign-pid block, cap, disabled-retry, per-exec
  counters, dead-pidfile), `test_retry_blocked_maps_unknown_and_
  holds_reservations`, `test_retry_policy_rendered_into_specs`.

**Retry policy surfacing (post-freeze fix `69e842d`):**
`retry_enabled`/`max_attempts` were rendered into specs but never
persisted into the immutable run_config snapshot — real submissions
silently defaulted to enabled/3. Fixed: snapshot stores both
(`dispatch.py` run_config), both run forms expose them with the
non-idempotent warning inline, execution detail shows the effective
policy. `test_retry_policy_persisted_in_snapshot` covers the full
create_execution→snapshot→spec chain for `retry_enabled=false`.

**Windows retry policy — implemented; real-host verification
pending.** NOT accepted. No confirmed-orphan resolution exists on
Windows, so automatic relaunch is DISABLED there — chosen option 2
(enforced disablement) of the policy set. Enforcement: (a)
`_retry_policy(posix=False)` forces `retry_enabled=false/
max_attempts=1` into the spec irrespective of run_config; (b) a
PowerShell `prep` step (`doStepIf=_win`) keeps
`.to_exec_attempt_<id>` and exits 70/`TO_RETRY_BLOCKED` on ANY
attempt > 1 — unconditional, so no spec can slip a relaunch
through. POSIX `rm`-based cleanup replaced by a PowerShell variant
on Windows (`rm` does not exist there — defect caught
pre-acceptance). Tests: `test_windows_spec_forces_retry_
disabled`, `test_win_prep_blocks_any_second_attempt`,
`test_win_and_posix_predicates_partition`,
`test_win_cleanup_uses_powershell_not_rm`. Kit row W6 is the
acceptance gate — on the real host it must verify: the
`.to_exec_attempt_<eid>` counter persists across the
cleanup/re-staging/retry sequence (file still present, contents
`2`); the second attempt is blocked in `prep` **before the test
CLI starts** (no `run`-step stdio, no pytest/robot process ever
spawned — `Get-Process` before/after reconnect); the resulting
**UNKNOWN** retains reservations and appears on `/reconciliation`
with the `buildbot_retry_attempt` audit. Until W6 passes on a real
host this row stays open.

Candidate under test: `feature/2.6.0-node-runners`, evidence below
recorded at commits `b70e73b` (slot-builders), `e859b52`
(unlaunched-cancel resolution + test fixes) and the PB-loss fix set
on live instance `/tmp/to251d` (imported tree
`/tmp/to251d/testorchestrator/`).

Environment: Buildbot master on 127.0.0.1:8010 (PB :9989), portal
:8443, runner `c260-linux-worker` (id `89413d40…`, `bbw-89413d40…`,
max_slots=2), SSH target `c251d-ssh` (`2f51617b…`, `to-ssh-dispatcher`).
Test fixture revisions are pinned per scenario. All runs dispatched
through Buildbot slot-builders (`to-target-<id>` + `to-target-<id>-s2`;
SSH `to-ssh-<id>` + `-s2`).

## Stage 5 — parallel execution

| Scenario | Evidence | Result |
|---|---|---|
| Same-runner concurrency | exec `14b68765` slot1 → `to-target-…-s2` build 1 (10:21:35→10:24:16); exec `aba78b56` slot0 → `to-target-…` build 99 (10:21:36→10:24:17). ~161 s simultaneous builds on one worker. | **PASS** — previously serialized per (builder,worker) pair |
| pytest/xdist overlap | bb:701 (earlier pass) + `6941ff8a`/`53d589d9` `-n2` runs on slots 0/1 — builds 106/8 both 11:01:18→11:02:39 | **PASS** — 4/4 cases, xdist workers concurrent per run |
| Playwright `--workers 2` | bb:711 — "Running 5 tests using 2 workers", 5/5 | **PASS** |
| Playwright shards | parent `d89c3cbe`: unit `a0e66f71` `--workers 1 --shard 1/2` ran {parallel a,b,c}; unit `0d2060a5` `--shard 2/2` ran {title check, arithmetic}. Frozen shard in unit snapshots; non-overlapping, complete coverage (5 tests). | **PASS** |
| Cypress partitions | parent `b1be38e5`: 3 spec units, units 0+1 overlapped (builds 3 @ s2 + 101 @ base), unit 2 queued → reused freed slot. Every spec assigned exactly once via frozen `unit.specs`. | **PASS** |
| Robot/Pabot overlap | bb:703 — two `robot -A` children concurrent (pabot_results/0,1,2 retained), merged output.xml/log.html/report.html + junit | **PASS** |
| Failure aggregation | parent `03de162a` → FAILED: unit `2768496e` (specfail) FAILED with junit (1 failed + 1 passed case) + failure screenshot; units `dc521f9c`, `4fe368d4` PASSED. | **PASS** — failed unit not hidden |
| Cancellation isolation | `af34121f` CANCELED mid-run (build 2 "cancelled (portal cancel request)") while `3bbc151b` ran to PASSED on slot 0 unaffected | **PASS** |
| Parent cancel (running+queued) | parent `3910f61b`: units `f0586fa5`/`631ca148` running → CANCELED (builds 4,103 cancelled); unit `4ccabb69` QUEUED → CANCELED without any build submitted; parent → CANCELED/INCOMPLETE | **PASS** — surfaced+fixed bug `e859b52` (`_resolve_unlaunched_cancels`) |
| Restart mid-parallel | portal **dispatch worker** (flask `testorchestrator worker`, pids 3770335/3770336) SIGKILLed ~11:01:4x during builds 106+8 overlap; both reconciled → COMPLETED/PASSED via durable reconcile. Killed: the portal-side dispatcher process only — master, PB workers, and remote CLIs untouched | **PASS** |
| Scheduled parallel | `sched-xdist` (once) → fired → `enabled=0`; exec `74109600` PASSED, snapshot preserved `parallel_mode=parallel, parallel=2`, frozen sha `8f0ba933`, item `pyxpar59` | **PASS** |
| Mixed-framework capacity | `max_procs=3`: pytest `-n2` (charge 2) `2e58e29d` dispatched; Robot `--processes 2` (charge 2) `9fc572e0` QUEUED (4>3); released → dispatched → both PASSED | **PASS** — process budget enforced + released |

## Worker PB loss (no master restart) — 11:12:33

Exact sequence (note: a first SIGKILL at 11:11:28 hit worker
`to-local` pid 3269820 — a *different* registered worker not serving
the build; the build was unaffected. The PB-loss interruption proper
followed at 11:12:33):

- Confirmed pytest CLI running on `bbw-89413d40` (pid 3773474)
  before interrupt; `kill -9` on worker pid 3246956
  (`/tmp/bbworker` — the process registered as `bbw-89413d40`).
  Master and all other processes untouched.
- Master log: `lostRemote`, `ConnectionLost`, worker detached from
  both slot builders; build 109 ended `results:retry` — Buildbot
  resubmitted the same buildrequest.
- Orphaned remote pytest 3773474 **survived** worker death and kept
  running unobserved (still alive at 11:13).
- Execution stayed DISPATCHED (lease renewed — genuinely uncertain,
  not silently failed/retried as a new submission).
- Worker restarted 11:13:37 → re-attached → retried build 110 ran
  the same buildrequest → success → reconcile → `5e4049aa`
  COMPLETED/PASSED.

**Historical gap on the 11:12 run (pre-fix):** the retry ran while
orphan 3773474 was still active in the same builddir — which attempt
produced the uploaded junit was not established, the orphan was
never resolved, and the retry was not recorded as a distinct
attempt. That row stands as pre-fix evidence only.

### PB-loss resolution — implemented + re-verified (post-fix)

Mechanism (all transports): `sh -c 'echo $$ > .to_exec_pid; exec
"$@"'` wrapper records the exec'd CLI pid at the builddir root;
`TO_EXEC_ID` is injected into the exec env so a later retry can
prove the recorded pid belongs to *this* execution (pid-reuse can
never hit an innocent process). Every build starts with a `prep`
step (POSIX-gated via spec `posix` flag — Windows workers get the
raw argv contract, no pidfile) that resolves prior-attempt pids:
`kill` only on env-marker match, reported either way
(`terminated orphaned prior attempt` / `already exited` / `alive
but env marker mismatch — left running`), pidfile removed, staging
dirs wiped so a lingering orphan cannot pollute the retried
workspace. Uploads go to `artifacts/<exec>/attempt-<buildno>/` so
each attempt's reports are attributable; the reconciler records
every build under the brid into `snapshot.bb_attempts` and audits
`execution.buildbot_retry_attempt`. Buildbot's automatic resubmit
(builder.py:457 `_resubmit_buildreqs`) is unconditional and cannot
be disabled — the design converts it into a *safe* resume: orphan
resolved synchronously before re-staging, workspace wiped,
attempts and artifacts distinct and audited.

- **PB worker retry** — exec `72123296`, brid 776: worker SIGKILL
  mid-`pytest` (pid 3790683, pidfile + `TO_EXEC_ID` verified);
  build 112 `lostRemote`→RETRY. Retry build 113 `prep` reported
  `prior attempt pid 3790683 already exited` (orphan self-
  terminated within ~50 s — captured stdout pipe death), wiped
  workspace, attempt-113 artifacts → PASSED. `bb_attempts` =
  [{112:RETRY,complete},{113:SUCCESS}] + retry audit event.
- **PB worker retry, longer dead-window** — exec `85bd3034`,
  brid 780: orphan 3791064 survived worker death; prep on build
  116 reported it already exited before re-staging; retry pytest
  = new pid 3791423, new pidfile — no double-execution.
- **SSH dispatcher loss (persistent remote orphan)** — exec
  `386af28a`, brid 783: dispatcher worker SIGKILL mid-run;
  remote `setsid` run 3791749 **persisted**. Retry build 634:
  prep resolved the dead local dispatcher pidfile, new
  dispatcher 3791835 reconnected and **resumed** remote run
  3791749 (same `run.pid`/pgid — no relaunch, true
  single-launch) → remote completed → collected under
  `attempt-634/` → PASSED; both attempts + audit recorded.
- The kill branch (`terminated orphaned prior attempt`) executes
  whenever an orphan outlives the restart gap — both PB orphans
  here self-terminated first; SSH remotes resume by design rather
  than being killed.

Distinct from: service restart, master SIGKILL (bb:645 earlier —
FAILED honest abort), SSH-session loss (bb:254 lineage).

## Code-level invariants verified

- Slot selection: busy slots skipped, freed slots recycled,
  `bb_slot` persisted per execution (test `test_slot_builders_and_pick`)
- `CANCEL_REQUESTED` + no `dispatch_id` + no `started_at` → CANCELED
  (never launched); parents excluded — rollup owns their outcome
  (test `test_queued_child_cancel_terminalizes_and_parent_rolls_up`)
- Combined capacity: job-slot CAS + `Σ requested_procs ≤ max_procs`
  CAS; charge model: sequential=1, parallel=requested, default
  playwright=unbounded on budgeted runner (config error), parent=0
- Rejected without dispatch: Cypress `parallel>0`, bad shard pairs,
  missing `opt_xdist`/`opt_pabot` probes in parallel mode

## CY-fail-over-SSH — current-candidate revalidation

- `ef962bb2` (binding `0ef16364`, sha `0389e85`, `c260-ssh-cypress`
  runtime on `c251d-ssh`): COMPLETED/FAILED — `spec2 failing
  intentional assertion failure` FAILED, `spec3 long-running`
  PASSED; per-case outcomes over SSH transport. bb:254 remains the
  historical record for the earlier candidate; this is the
  revalidation row, not a relabel.

## Queue & resource policies — live verified (12:10–12:19)

Evidence on live instance (`/tmp/to251d`, working tree synced —
not yet clean-package):

- **Slot capacity + wait reasons**: 3 sequential runs on
  `max_slots=2` — `f8579d42`/`870355f4` DISPATCHED (slots 0/1),
  `939da91d` QUEUED with `wait_reason="capacity: runner
  'c260-linux-worker' job slots full (2/2 in use)"`; on release it
  redrove FIFO → DISPATCHED slot 1 → COMPLETED.
- **Reservation ledger**: `939da91d` acquired `resource_reservations`
  rows `slot` idx 1 + `procs` qty 1 at dispatch; on COMPLETED both
  `released_at` set, `release_state='terminal'`.
- **Procs budget + starvation avoidance**: `max_procs=3`; strays
  `8b99b7a2`+`f82dae23` and fillers `462a79f6`/`929b0c54` (charge 2
  each) queued behind full capacity while charge-1 `997e16e0`
  (created later) was admitted — deeper-scan FIFO skips
  head-of-line-blocked jobs instead of starving them.
- **Queue deadline**: `462a79f6` `queue_deadline_at` passed while
  blocked → FAILED `error_kind='queue'` ("queue deadline exceeded —
  still waiting (…); increase runner capacity or
  queue_timeout_seconds") + audit `execution.queue_deadline_expired`.
  Deadline applies to QUEUED only; dispatched runs unaffected.
- **Schedule misfire**: schedule `1c47d7c8` 1801 s overdue vs 10 m
  interval, `misfire_policy=skip` → occurrence dropped, `next_run_at`
  advanced one interval, audit `schedule.misfire_skipped` — no
  catch-up storm, no double-dispatch.
- **Explicit invalid runner**: execution pinned to nonexistent
  runner id → immediate FAILED "no fallback to another target"
  (fail-fast for explicit targets; wildcard waits instead).
- **UNKNOWN retains resources** (code invariant): `UNKNOWN ∈
  _ACTIVE_STATES` (slot+procs charge held) and `UNKNOWN ∉
  _TERMINAL_STATES` (reservation rows stay open) — capacity stays
  blocked until operator reconciliation. A connection loss never
  frees a slot/environment while the original may still be using it.
- **Two distinct deadlines**: `queue_timeout_seconds` →
  `queue_deadline_at` → FAILED `error_kind='queue'` (only while
  QUEUED, verified above) vs `timeout_seconds` → runtime deadline
  enforced inside the invocation (`exec_timeout`, tree force-killed
  before slot release) → FAILED run outcome. Distinct kinds,
  distinct surfaces.
- **Time zones / DST — not applicable by design**: schedules are
  `once` (absolute UTC `next_run_at`) or `interval` (`now +
  interval_minutes` duration arithmetic). There is no wall-clock
  cron kind, so no schedule can be DST-shifted; every comparison is
  absolute-time UTC. If wall-clock scheduling is ever added it will
  need an explicit tz field + DST policy — recorded here so the
  requirement isn't silently forgotten.
- Regression coverage: `test_fifo_redrive_skips_blocked`,
  `test_misfire_skip_policy`, `test_reservations_*`,
  `test_queue_deadline_*` (63 passed).

## CI API + webhooks — live verified (12:26–12:30)

`/api/v1` bearer-token surface + durable outbound notifications
(migration `z8a9b0c1d2e3`; token `ci-live-check` scope `run`):

- `POST /api/v1/executions` → `abca0eac` created from discovered item
  `pyxpar59`, `client_ref="ci-live-001"` persisted, `deployed_rev`
  stamped in response.
- Idempotent replay: identical `client_ref` POST returned the SAME
  execution id with `idempotent_replay=true` — no double-launch.
- `GET /executions/<id>` → `dispatched`, `bb_slot=0`,
  `attempts=[{buildid:796, number:122}]` — Buildbot attempt chain
  visible through the API.
- Run completed PASSED; `_enqueue_terminal_notifications` fanned out;
  `webhook` outbox events delivered to a local receiver with
  `X-TO-Event: execution.completed` + `X-TO-Signature: sha256=…`
  (HMAC key in the encrypted secret store — never inline).
- Two deliveries = two genuinely-terminal executions (`68e44b0a` SSH
  + `abca0eac`) — not a double-enqueue; `notified_at` stamps prevent
  re-fan-out, and zero-endpoint sweeps mark executions so
  later-created endpoints never replay history. Upgrade backfills
  `notified_at` for already-terminal rows.
- Regression coverage: `tests/test_api_ci.py` (10) — auth 401s,
  read-vs-run scope, project scoping (403 trigger / invisible read),
  cancel, retry-chain, cases/artifacts, enqueue filter+scope,
  signed delivery, retry-on-failure.
- CSRF exemption scoped to `api_bp` only (bearer, no cookies).
- Full suite: 734 passed (env-blocked: licence-key file, 2 Playwright
  UI files, `test_cancel_kills_process_tree` pgrep-flake vs live
  `slow-kre` from SSH run `3891f37d`).

## Reconciliation UI + manifests — live verified (12:47–13:04)

- `/reconciliation` admin view lists ambiguous executions (UNKNOWN /
  stale-cancel / orphaned-DISPATCHED) with held capacity + age; real
  UNKNOWN row `4613887f` rendered (commit `adc7945`).
- Sealed evidence manifests: `_collect` writes `manifest.json` into
  report_dir, registers it `kind=manifest`; API
  `GET /executions/<id>/manifest` re-verifies every listed file
  (existence + root-containment + SHA-256).
- **Bug found + fixed** (`c7642ac`): `kre-*.log` artifacts registered
  as `logs/…` relative to report_dir but stored under workspace root —
  permanently undownloadable (404). `_collect` now mirrors logs into
  `report_dir/logs/`; live re-run `1e2edef6`: 3 artifacts, 0
  missing/mismatched, log download 200.
- History matrices (commit `cf1e932`): `/suites/<id>/matrix` and
  `/discovered/<id>/matrix` — case_ref rows × 15 recent terminal
  executions; flaky rows and persistent failures ranked first; skips
  stay skips, unresolved marked. Live item `406c580c`: 15 execs,
  real case rows.

## Backup/restore — live verified (13:06–13:09)

- `backup_state` on the live instance → `backup-20261120T120654Z.tar.gz`.
- **Bug found + fixed**: instance contains an installer-generated
  absolute symlink (`buildbot/master/master.cfg` →
  `/abs/…/data/buildbot/master.cfg`); `tarfile` `data` filter rejects
  absolute links → backups were unrestorable. `recover` now rewrites
  absolute links that resolve inside the archive to relative form and
  skips links pointing outside it.
- Live restore: 11,142 members extracted; table counts match exactly —
  executions 816, test_results 722, artifacts 2927, users 1, runners 2,
  schedules 7; link restored as relative `../master.cfg`; restored
  instance boots (`create_app`) with secret store intact.
- Regression coverage: `test_absolute_symlink_restored_relative`,
  `test_absolute_link_to_outside_member_skipped` (+14 existing).
- Orphan-evidence lifecycle (commit `ef18039`): `find_orphans` /
  `prune_orphans` existed but were never invoked — orphan dirs are
  now listed on `/reconciliation` with an explicit audited prune
  action (managed-root + no-symlink + live-ref guards re-checked at
  delete time). Live dry-run on the current instance: 0 orphans.

## Representative 2.5.1→2.6.0 upgrade — verified (13:09–13:11)

Real 2.5.x instance (`/tmp/to251c/data`, alembic `s9t0u1v2w3x4`)
copied and migrated to head `z8a9b0c1d2e3` via `flask db upgrade`
(the same `toctl db-upgrade` path the installer ExecStartPre runs):

- 7 migrations applied cleanly (runtime_kind → parent/child →
  probe_json → max_procs → bb_slot → queue policies → CI API).
- ALL pre-existing data preserved: audit_events 40, settings 12,
  users 1, outbox_events 5, projects 1, runners 1, secret_refs 4,
  suites 1 + entries, executions 5, artifacts 8, runtime_connections 1.
- All 2.6.0 structures created: resource_reservations, api_tokens,
  webhook_endpoints, wait_reason, queue_deadline_at, client_ref,
  notified_at, misfire_policy.
- **Bug found + fixed**: the `notified_at` backfill's `op.execute`
  UPDATE never committed — `transactional_ddl=False` + SA 2.x
  autobegin means raw DML needs `autocommit_block()`. Reproduced on a
  second copy, fixed, re-verified: all 5 pre-existing terminal rows
  marked notified (prevents stale-event storm on first webhook).
- Post-upgrade boot OK; ORM reads all 5 executions.
- Note: two 2.5.x-era migrations contain the same non-committing DML
  pattern, but only for inert settings cleanup — already applied on
  all deployed DBs; left as-is.

## Full-suite results — separately attributed runs

Run A — **batch full suite at `8fc0ae5`** (11m53s):
**751 passed, 11 failed, 1 skipped.** Failures: 10× modal
Playwright tests targeting the absent `:39995` preview; 1×
`test_production_key_auto_loaded` (external licence-issuer key,
absent by design). Kept as recorded — NOT re-scored.

Run B — **targeted rerun of the 10 modal tests at `8fc0ae5`** after
correcting the preview environment (seeded preview recipe below;
replaced a stale nov19 preview holding an empty fixture DB):
**10 passed in 46s.** This is a separate run — it does not retro-
edit Run A's tally.

Still open gap: `test_production_key_auto_loaded` remains
environment-blocked (external key required) — recorded as a gap,
not a pass. The 1 skipped test is a separately identified skip.

Run C — **batch full suite at `57445bc`** (post project-access +
retry-surfacing + Windows-prep changes; 8m08s):
**781 passed, 3 failed, 1 skipped.** Per-failure accounting — each
linked to its Run D outcome; no combined tally is claimed:

| Run C failure | Cause | Run D outcome | Remediation |
|---|---|---|---|
| `test_infra_controls_browser::test_second_runner_selected_persists` | Preview fixture: DB had only `preview-worker`; the test selects `option.nth(2)` (needs `rem-1`/`rem-2` remote runners) | **passed** (rerun) | fixture-only: seeded `rem-1`/`rem-2` remote runners into the preview DB — **no application code changed** |
| `test_infra_controls_browser::test_runner_selection_survives_validation_error` | Same fixture gap (same `nth(2)` selection) | **passed** (rerun) | fixture-only, same seeding — **no application code changed** |
| `test_production_key_auto_loaded` | External licence-issuer key absent — environmental by design | **still blocked** — recorded as a gap, not a pass | none possible in this environment; requires the external key |

Run D — **targeted reruns at `57445bc`** of the environment-fixed
tests: 5/5 infra pass (13s) — the whole
`test_infra_controls_browser` module, not only the two failures.
The batch tally stands as recorded; reruns are separate rows.
Historical note (superseded by the logging implementation): between
Run C and `adf4490` only docs/ledger commits landed, so Run C was
the full-suite evidence at that head. **`d6c14b3` adds application
code (logging exports + structured output)** — Run E below carries
the new full-suite result; Run C/D evidence stays attributed to
`57445bc`. The packaged-candidate full suite is retained as a
required row in `docs/packaged-acceptance.md` §6. Earlier evidence
stays attributed to its own revision (734-passed pre-retry-hardening;
751-passed batch at `8fc0ae5`; 71-targeted acceptance suites).

Run E — **batch full suite at `d6c14b3`** (logging implementation
head; 7m18s): **806 passed, 2 failed, 1 skipped.** Per-failure
accounting — no combined tally:

| Run E failure | Cause | Outcome | Remediation |
|---|---|---|---|
| `test_production_key_auto_loaded` | External licence-issuer key absent — environmental by design | **still blocked** — recorded as a gap, not a pass | none possible in this environment; requires the external key |
| `test_pipeline::test_cancel_kills_process_tree` | Transient stray-process timing (`pgrep -f slow-kre` matched a tearing-down tree) | **passed** on isolated rerun (0.65s) | none — cancel path untouched by `d6c14b3`; flake retained as evidence |

Run E covers the logging acceptance tests (`test_log_exports.py`,
20 tests — all pass inside the batch) plus every regression touched
by `record_audit`/`emit` callers.

`TO_INSTANCE_DIR=/tmp/to-preview-data`, waitress on :39995, seeded:
User Admin/`Review-Preview-#2026!`, Project `62d2f3e1b4654e5680fd187795c454f7`,
GitBinding `authoring_writable=true`, one local Runner + pytest
RuntimeConnection, **two remote runners `rem-1`/`rem-2`
(connection_mode=remote, target_type=buildbot-worker)** for the
infrastructure browser tests, and `import testorchestrator.logs`
before `db.create_all()` so `operational_logs` exists (the
create_all path doesn't import the model — production gets it via
migrations; the modal/infra tests 500 without it). Modal buttons
need `can_author` (binding authoring flag) — an empty-seed instance
404s/omits them.

## Open
- ~~Worker PB-loss retry/orphan correctness~~ — **resolved**; see
  "PB-loss resolution — implemented + re-verified" above. Retry
  boundaries hardened (confirmed-terminate, mismatch-block,
  explicit retry_enabled/max_attempts, TO_RETRY_BLOCKED→UNKNOWN).
- Windows retry safety: **implemented; real-host verification
  pending** — enforced disablement (`d4c7e85`), acceptance gate is
  kit W6. Not claimed accepted.
- Windows matrix: Robot + pytest on a real Windows host —
  **blocked infrastructure**: no Windows host exists in this
  environment; nothing remains implementable on the Linux side.
  Repeatable kit prepared and kept ready (W1–W14 incl. W6 retry
  policy, W13 paths-with-spaces, W14 service account):
  `docs/windows-acceptance-kit.md`. On host availability: run
  W1–W14, fix discovered defects, then the packaged matrix.
- Final clean-package eight-combination smoke matrix — pending
  Windows. Procedure defined: `docs/packaged-acceptance.md`.
- ~~Final-head full suite~~ — Run C at `57445bc`: 781 passed,
  3 failed (2 preview-fixture → fixed+rerun pass; 1 external-key
  gap). Recorded separately from earlier runs. Packaged-candidate
  suite remains a `packaged-acceptance.md` §6 row.
- Logging implementation (`d6c14b3`): CSV export + structured output
  accepted per `docs/packaged-acceptance.md` §5a; Run E below is the
  full-suite result on that head. Windows-side logging checks fold
  into the W-matrix row.

## Project access (membership) — implemented post-freeze under the
## approved access change

Authorization = role AND project membership (`User.allowed_projects`
— the existing §16 seam, semantics corrected: empty = none for
non-admins, Administrators bypass through the role). Enforcement:

- `User.project_allowed`: admin → all; others → assigned list only
  (`models.py`). No new/competing permission system introduced.
- Portal: `_project_or_404`/`_execution_or_404` per request
  (revocation on next request); executions list + filter dropdowns +
  schedules/frameworks scoped at query level; reconciliation rows
  already scoped; projects list shows a proper empty state.
- API: `api_token_required` resolves the token owner each request —
  deactivated owner → 401; `run`-scope token whose owner cannot run
  → 403; project scope = token scope ∩ owner's CURRENT membership
  (`_api_project_allowed`/`_api_project_ids`). Service tokens whose
  owner is not a user keep their own scope.
- Dispatch boundary (`_creator_still_allowed` in `run_execution`):
  pending work whose submitter lost membership/was deactivated — or
  whose `api:<name>` token was revoked/out-scoped — fails visibly
  `error_kind='access'` + `execution.access_blocked` audit; it never
  launches. Schedule-fired runs carry the creator's identity, so
  member-removed users' schedules stop launching (defined policy —
  not anonymous system work).
- Admin UI both views: System → Users → Edit → Project access
  (search/select-all/clear/role shown; admins see "All projects
  through Administrator role") and Project → Access (inverse
  matrix). Every change audited `user.project_access` with
  before/after lists. Users page flags unassigned users — the
  upgrade review workflow (nothing is silently re-granted; prior
  explicit lists are preserved).
- Backup/restore: `allowed_projects` is a users-table column —
  covered by the existing backup path (row counts verified earlier).

Acceptance: `tests/test_project_access.py` — 17 tests, 2 projects ×
2 same-role users differing assignments, isolation via URL/API/
export/artifact/ID-substitution, no role elevation, empty state,
mid-session revocation, token ∩ owner semantics, dispatch-boundary
block, admin UI roundtrips + audit. Two existing tests updated for
the corrected semantics; runbook form-posts keep retry default via
the hidden-field contract.
