# Legacy TestOps execution-feature comparison — audit

HEAD: `bed34e9` (doc-only change on `57e2211`). Status labels:
**impl** = code exists; **fixture** = test-verified; **partial** =
exists but scoped down; **absent** = not implemented; **unverified** =
impl without evidence.

## A. Reusable execution configurations

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Named saved config independent of schedule | none — `run_config` lives only inside schedule JSON + launch form | grep: no ExecutionConfig model | **absent** |
| Config fields: repo+rev, suite, profile, browser, runtime, timeout, retry | `run_config` = profile/browser/remote_webdriver_*/timeout_seconds/kre_options; repo+rev via binding+sha | `tests/test_pipeline.py` snapshot assert | partial — no named object |
| Edit/duplicate | schedules editable? form is create-only (project_detail.html:140+) | impl | partial |
| "Run now" on schedule | no route — `fire_due_schedules` only fires due rows | grep routes | **absent** |
| Reusable vs immutable | run_config (mutable until fire) → snapshot (immutable at `create_execution`) | snapshot tests | impl |

**Impact:** operators must re-enter run settings per launch; no
saved-config reuse between manual/schedule/plan.

## B. Scheduling

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Once + interval | `kind` select + `interval_minutes` | schedule tests | impl |
| Calendar rules (weekly/cron) | none — integer minutes only | forms.py | **absent** |
| Start/end date | `run_at` UTC only; no end date | forms.py | **absent** |
| Timezone/DST | UTC-only (explicit; no pick) | field help | partial — honest, no tz |
| Enable/disable | `enabled` flag | impl | impl |
| Missed-run | fires late next tick, audited | documented + tests | impl |
| Overlap policy | none specified — a due interval fires regardless of previous run state | impl | **absent** (undefined) |
| Next-run display | project page + dashboard upcoming | UI | impl |
| Calendar view | none | templates | **absent** |
| Manual trigger | none | routes | **absent** |
| Ownership/perms | created_by + project scope | tests | impl |

## C. Runtime/environment selection

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Explicit runtime/version pick | none — `pick_runtime` auto-selects compatible+enabled | impl | **absent** (auto only) |
| Selected vs actual version evidence | `detected_version` recorded; gated options drop w/ evidence | option-gating tests | impl |
| Browser/headless selector | `SUPPORTED_BROWSERS` select | fixture | impl |
| Remote WebDriver | url+type fields → `-browserType=Remote`+args | fixture | impl |
| Project capabilities | deferred — KRE project owns them | doc | absent (deferred) |
| Profiles | free-text `execution_profile` | impl | impl |
| Collection browser/profile | KRE-side, not rewritten (arg-fixture only) | doc | partial/unverified |
| Runner pick | auto eligible pool | impl | impl |
| Compatibility validation | browser vs os_arch; runtime vs runner | launch-time validate | impl |
| Unknown runtime | `detected_version` empty → gated options dropped honestly | tests | impl |

## D. Agents/capacity/distribution

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Direct runner pick | none — auto pool only | impl | absent (by design) |
| Eligible pool + health/draining/platform | `pick_runner` + heartbeat + draining + os_arch | tests | impl |
| Atomic capacity reservation | `_assign_runner` CAS (all paths) | multi-process tests | **fixture-verified** |
| Licence allowance | `license_slots` cap in same CAS | `test_license_allowance_caps_claim` | impl |
| Load-balancing policy | first-eligible (no policy) | impl | partial |
| Reassign queued unstarted | QUEUED has no runner until CAS — repicks on retry | impl | impl |
| Duplicate pickup | `tasks/<id>/claim` CAS | `test_remote_pickup_claim_exactly_once` | impl |
| Agent compat | capability gate at register/tasks | `test_agent_capability_gate` | impl |
| Stale agent | reconcile → UNKNOWN | impl | impl |
| UNKNOWN resolution | admin CAS + reason | `test_resolve_unknown_releases_capacity` | impl |
| Collection parallelism | KRE-side, invisible | doc | unverified |
| Case sharding | none | — | out of scope |

## E. Execution control

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Cancel pending | atomic CAS | `test_cancel_between_claim_and_launch_aborts` | impl |
| Stop running | CANCEL_REQUESTED → killpg (local fixture-verified; remote unverified) | `test_cancel_kills_process_tree` | partial |
| Timeout | `timeout_seconds` → `timeout_s` | `test_timeout_kills_after_limit` | impl |
| Bulk cancel | none | routes | **absent** |
| Retry | new execution + attempt lineage | impl | impl |
| Rerun-failed | none | routes | **absent** |
| Rerun original revision | retry uses same suite — snapshot sha preserved? retry calls create_execution with binding's CURRENT sha | impl | partial — current, not original |
| Stop-on-failure | plan `on_failure` policy | plan tests | impl |
| Restart recovery | reconcile → UNKNOWN (not silent retry) | impl | impl |

## F. Execution history/evidence

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Dedicated history view | `/executions` = last-100, project-scoped | impl | partial |
| Search/filter (name/id/project/date/state/outcome/actor/runner/browser/schedule/plan) | none on executions page; audit has text/date/type | impl | **absent** for history |
| Stable sort/pagination | created_at desc, no pagination | impl | partial |
| Attempt lineage | attempt/retry_of links | impl | impl |
| Collection drill-down | TestResult rows where reported | impl | partial |
| Logs/screenshots/reports/artifacts | artifact store + checksums + report links | impl | impl |
| Retention/archive/delete | none | impl | **absent** |
| Partial/missing artifacts | shown via manifest; failed-upload state | impl | impl |

## G. Notifications/metadata

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Failure / infra-error / UNKNOWN / launch-failure / completion notifications | none — no notifier subsystem | grep | **absent** |
| Recipient/subscription config | none | — | **absent** |
| Execution names/tags/custom fields | no name/tags on Execution; created_by + snapshot metadata | model | partial |

## H. Additional execution types

| Item | Implementation | Evidence | Status |
|---|---|---|---|
| Legacy Katalon Command | none — KRE console args only | kre.py | **absent** |
| Generic Command | none | — | **absent** (deferred) |

## Prioritized gaps

### Required before execution-replacement acceptance
1. **Explicit runtime pick** — auto-only means an operator cannot pin
   a tested KRE build; multi-runtime runners are ambiguous.
2. **Run-now on schedule** (same config path) — the missing link
   between saved config and manual launch.
3. **Overlap policy** — an interval schedule's second fire while the
   first still runs is undefined.
4. **Bulk cancel** — operators can't clear a backlog safely.

### Useful follow-up
- Named reusable execution-config entity (+duplicate/edit/run).
- Calendar rules, end date, timezone display, calendar view.
- Rerun-failed + rerun-original-revision distinction.
- Execution-history filters/pagination (project, state, outcome,
  date, actor, runner).
- Load-balancing policy beyond first-eligible.
- Retention/archive policy for executions + artifacts.

### Intentionally deferred
- Notifications (failure/infra/completion), recipients, webhooks.
- Execution tags/custom fields.
- Katalon-Command/Generic-Command execution types.
- Desired-capabilities editor (KRE project owns capabilities).
- Case-level sharding; collection-parallelism controls.
- General test-management/release-management parity.

### Access blockers (unchanged)
Authorized second host + connectivity; licensed KRE (Stage B);
agent token issuance on the target runner.
