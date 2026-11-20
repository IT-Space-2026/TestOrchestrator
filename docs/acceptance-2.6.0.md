# TestOrchestrator 2.6.0 — acceptance evidence ledger

**Stage 5 status: parallel + recovery scenarios exercised; PB-loss
retry/orphan correctness implemented and re-verified (commit
pending).** The earlier "same brid re-ran → PASSED" row proved only a
final outcome. The mechanism is now: per-attempt pidfile +
`TO_EXEC_ID` env marker (kills are env-verified), retry-prep orphan
resolution, per-attempt `attempt-<buildno>` artifact dirs, portal-side
`bb_attempts` snapshot tracking + `buildbot_retry_attempt` audit.
Re-verified live on both transports — see "Worker PB loss" below.
The original 11:12 run remains marked as the pre-fix evidence it is.

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

## Open
- **Worker PB-loss retry/orphan correctness** — release-blocking;
  the retry ran with the orphan still active in the shared
  builddir, attempt provenance of uploaded results unestablished,
  retry unrecorded as an attempt. Fix: pidfile orphan resolution at
  retry prep + per-attempt artifact dirs + portal attempt tracking.
- Windows matrix: Robot + pytest on a real Windows host — release
  blocker, unverified (no host).
- Representative 2.5.1 upgrade + restore acceptance.
- Remaining release milestones: resource locks, reconciliation UI,
  history/matrices, CI APIs/webhooks, artifact lifecycle, maintenance
  controls, dashboards/docs; final clean-package eight-combination
  smoke matrix.
