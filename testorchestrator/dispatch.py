"""Execution pipeline: dispatch, state machine, local runner (§4–§8).

Workflow: prepare -> execute -> collect -> normalize -> finalize.
Collection continues after failed tests; the final verdict comes from
recorded execution evidence, not from a wrapper that hides failures.

Durability: execution rows + outbox events are committed transactionally
before any side effect, so a worker/portal restart reconciles rather than
duplicating work (A06). An ambiguous state becomes UNKNOWN — never a blind
re-dispatch of a potentially destructive run.

Dispatch and collection run in `flask worker`, outside request handlers.
"""

import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app

from . import settings as settings_mod
from .adapters import kre as kre_adapter
from .adapters.dagu import DaguAdapter, NotConfigured as DaguNotConfigured
from .execution import (Artifact, DiscoveredItem, Execution,
                        ExecutionState, GitBinding,
                        OutboxEvent, Outcome, Plan, Runner,
                        RuntimeConnection, Schedule, Suite, SyncState,
                        TestResult, utcnow)
from sqlalchemy.exc import OperationalError

from .extensions import db
from .models import record_audit
from .results import aggregate_outcome, entry_state, parse_junit
from . import results as results_mod

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15


class DispatchError(RuntimeError):
    pass


# ---------- creation / snapshot ----------

def create_execution(username: str, suite: Suite = None,
                     binding: GitBinding = None,
                     plan_id: str = "",
                     source_sha: str = "", run_config: dict = None,
                     extra_snapshot: dict = None,
                     schedule_id: str = "",
                     source_item=None) -> Execution:
    """Create an immutable execution snapshot (§4, A07). Exactly one
    target: a TO suite OR a repository DiscoveredItem — never both,
    never neither. Repo-def runs snapshot their durable identity
    (binding+kind+path+sha) so history survives inventory replacement."""
    if bool(suite) == bool(source_item):
        raise DispatchError(
            "exactly one execution target required: suite or "
            "source item")

    rc = dict(run_config or {})
    if source_item is not None:
        rc.setdefault("suite_path", source_item.path)
        rc.setdefault("collection", source_item.kind == "collection")
    snapshot = {
        "plan_id": plan_id,
        "plan_run_id": (extra_snapshot or {}).get("plan_run_id", ""),
        "source_target": ({
            "binding_id": source_item.binding_id,
            "item_id": source_item.id,
            "kind": source_item.kind,
            "path": source_item.path,
            "name": source_item.name,
            "sha": source_item.sha,
        } if source_item is not None else None),
        "suite_id": suite.id if suite else "",
        "suite_name": (source_item.name if source_item is not None
                       else suite.name),
        "suite_version": suite.version if suite else 0,
        "suite_hash": suite.content_hash if suite else "",
        "entries": ([
            {
                "order": e.order,
                "case_ref": e.case_ref,
                "enabled": e.enabled,
                "config": json.loads(e.config_json or "{}"),
            }
            for e in suite.entries
        ] if suite else []),
        "run_config": {
            "profile": str(rc.get("profile") or "default")[:128],
            "browser": str(rc.get("browser") or "Chrome (headless)")[:128],
            # Remote WebDriver (opt-in): browser runs on the WebDriver
            # service, not the KRE host — overrides -browserType=.
            "remote_webdriver_url":
                str(rc.get("remote_webdriver_url") or "")[:512],
            "remote_webdriver_type":
                str(rc.get("remote_webdriver_type") or "")[:64],
            # Requested execution target ("" = auto pick at dispatch).
            "requested_runner": str(rc.get("runner_id") or "")[:32],
            "requested_runtime": str(rc.get("runtime_id") or "")[:32],
            "collection": bool(rc.get("collection")),
            "suite_path": str(rc.get("suite_path") or "")[:512],
            # Running-time limit in seconds (process-launch clock,
            # not queue time); absent/0 = adapter default 3600.
            "timeout_seconds":
                int(rc.get("timeout_seconds") or 0),
            # Requested version-gated KRE options — applied only if the
            # detected runtime version supports them (adapters/kre.py).
            "kre_options": {
                str(k)[:32]: str(v)[:64]
                for k, v in dict(rc.get("kre_options") or {}).items()},
            # Operator acknowledgement for gated-option drops — opt-in,
            # audited in kre-gating.log when used.
            "kre_options_allow_drop":
                bool(rc.get("kre_options_allow_drop")),
        },
        "binding_id": binding.id if binding else None,
        "source_sha": source_sha,
        "created_by": username,
    }
    # disabled runtime connections must not launch — selectors
    # filter them, but a direct submission is rejected here too.
    rt_id = (run_config or {}).get("runtime_id")
    if rt_id:
        from .execution import RuntimeConnection as _RC
        _rt = db.session.get(_RC, rt_id)
        if _rt is None or not _rt.enabled:
            raise DispatchError(
                "runtime connection unavailable or disabled")
        # Remote installation -> the Dagu-worker transport only, never a
        # portal-host invocation. Incomplete bindings (no coordinator
        # endpoint, no executable path) reject here rather than
        # mid-flight — there is no silent local fallback.
        if _rt.worker_label:
            if not _rt.executable_path:
                raise DispatchError(
                    "remote execution is not configured/available — "
                    "worker-bound installation has no executable path")
            if not _dagu_configured():
                raise DispatchError(
                    "remote execution is not configured/available — "
                    "this installation is bound to a Dagu worker but "
                    "no Dagu coordinator endpoint is configured")
            snapshot["dispatch_transport"] = "dagu-worker"
    snapshot.update(extra_snapshot or {})
    execution = Execution(
        project_id=(suite.project_id if suite
                    else source_item.project_id),
        suite_id=suite.id if suite else None,
        source_item_id=source_item.id if source_item else None,
        binding_id=(binding.id if binding else
                    (source_item.binding_id if source_item else None)),
        snapshot_json=json.dumps(snapshot),
        source_sha=source_sha,
        created_by=username,
        schedule_id=schedule_id or None,
    )
    db.session.add(execution)
    db.session.flush()  # id assigned before outbox references it

    # Dispatch route: Dagu when configured, else the local runner outbox.
    # Persisted in the same transaction as the execution (§7).
    kind = "dagu-dispatch" if (_dagu_configured() or _dagu_mandatory()) \
        else "local-dispatch"
    db.session.add(OutboxEvent(
        kind=kind, payload_json=json.dumps({"execution_id": execution.id})))
    db.session.commit()
    return execution


def queue_plan_executions(username: str, plan: Plan,
                          binding: GitBinding = None,
                          source_sha: str = "",
                          entries: list = None,
                          plan_run_id: str = "",
                          schedule_id: str = "",
                          run_config: dict = None) -> tuple:
    """Queue one execution per plan entry under a shared plan_run_id —
    the durable handle that lets summaries, stop-on-failure and
    rerun-failed group attempts of the same run (§10)."""
    run_id = plan_run_id or new_plan_run_id()
    exs = []
    for entry in (entries if entries is not None else plan.entries):
        exs.append(create_execution(
            username, entry.suite, binding=binding, source_sha=source_sha,
            plan_id=plan.id, run_config=run_config,
            extra_snapshot={"plan_run_id": run_id},
            schedule_id=schedule_id))
    return run_id, exs


def new_plan_run_id() -> str:
    import uuid
    return uuid.uuid4().hex


def retry_execution(username: str, ex: Execution) -> Execution:
    """Queue the NEXT attempt as a new immutable execution (§10):
    retry_of links the chain, attempt increments, plan_run_id is kept so
    plan aggregation counts the latest attempt — never a rerun/mutation
    of the original row."""
    terminal = (ExecutionState.COMPLETED, ExecutionState.FAILED,
                ExecutionState.CANCELED, ExecutionState.UNKNOWN)
    if ex.state not in terminal:
        raise DispatchError(
            "only terminal executions can be retried — cancel first")
    snap = ex.snapshot()
    binding = (db.session.get(GitBinding, ex.binding_id)
               if ex.binding_id else None)
    new_ex = create_execution(
        username, ex.suite, binding=binding, source_sha=ex.source_sha,
        plan_id=snap.get("plan_id") or "",
        run_config=snap.get("run_config") or {},
        extra_snapshot={
            "plan_run_id": snap.get("plan_run_id") or "",
            "retry_of": ex.id,
            "attempt": int(snap.get("attempt") or 1) + 1,
        })
    record_audit(username, "execution.retry_queued",
                 f"{new_ex.id} (retry of {ex.id}, attempt "
                 f"{int(snap.get('attempt') or 1) + 1})")
    return new_ex


def _dagu_configured() -> bool:
    return bool(settings_mod.get("dagu_endpoint"))


def _dagu_mandatory() -> bool:
    """Owner decision (Option B): Dagu is the required orchestrator.
    Deployments may disable only via an explicit setting — not silently."""
    return bool(settings_mod.get("dagu_mandatory", True))


def request_cancel(execution_id: str, username: str, reason: str = "") -> bool:
    """Cancel targets the actual process tree (§6); stays CANCEL_REQUESTED
    until termination is acknowledged — uncertainty is surfaced."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        return False
    if ex.state in (ExecutionState.COMPLETED, ExecutionState.FAILED,
                    ExecutionState.CANCELED):
        return False
    if ex.dispatch_id:
        try:
            if (ex.snapshot().get("dispatch_transport")
                    == "dagu-worker"):
                DaguAdapter.from_settings().stop(
                    DaguAdapter.REMOTE_DAG_NAME, ex.dispatch_id)
            else:
                DaguAdapter.from_settings().cancel(ex.dispatch_id)
        except Exception:
            pass  # flag + lease expiry still bound the run
    ex.state = ExecutionState.CANCEL_REQUESTED
    ex.cancel_reason = reason[:256]
    db.session.commit()
    record_audit(username, "execution.cancel_requested", execution_id)
    return True


# ---------- runner selection (§6) ----------

def capacity_limit(runner: Runner) -> int:
    """Dispatch ceiling = min(hardware slots, declared licence seats).
    Unset licence means unconstrained, never an assumed seat count."""
    if runner.license_slots:
        return min(runner.max_slots, runner.license_slots)
    return runner.max_slots


def effective_slots(runner: Runner) -> int:
    """Free capacity = capacity_limit - currently leased executions.
    A floating KRE licence is consumed per concurrent SESSION — the
    limit here prevents dispatching more KRE processes than either the
    hardware or the licence allowance permits (never multiplied)."""
    active = Execution.query.filter(
        Execution.runner_id == runner.id,
        Execution.state.in_(_ACTIVE_STATES),
    ).count()
    return max(0, capacity_limit(runner) - active)


def pick_runner(execution: Execution) -> Runner | None:
    """Enabled, non-draining runners with project scope and free slots —
    local and remote modes both eligible (remote requires a live agent)."""
    runners = Runner.query.filter_by(enabled=True, draining=False).all()
    for runner in runners:
        allowed = json.loads(runner.allowed_projects or "[]")
        if allowed and execution.project_id not in allowed:
            continue
        if effective_slots(runner) <= 0:
            continue
        return runner
    return None


def pick_runtime(execution: Execution, runner: Runner):
    for rt in runner.runtimes:
        if not rt.enabled:
            continue
        allowed = json.loads(rt.allowed_projects or "[]")
        if allowed and execution.project_id not in allowed:
            continue
        return rt
    return None


# ---------- worker ----------

def _cancel_check(execution_id: str):
    """Polls the durable cancel flag — the DB is the only authority, so a
    request-handler cancel reaches a worker in another process."""
    def check() -> bool:
        state = db.session.execute(
            db.text("SELECT state FROM executions WHERE id = :i"),
            {"i": execution_id},
        ).scalar()
        # db.Enum persists member names, not values.
        return state == ExecutionState.CANCEL_REQUESTED.name
    return check


class _DbCancelEvent:
    def __init__(self, execution_id):
        self.execution_id = execution_id
        self._check = _cancel_check(execution_id)

    def is_set(self):
        try:
            return self._check()
        except Exception:
            return False


def _transition(ex: Execution, state: ExecutionState) -> None:
    ex.state = state
    ex.heartbeat_at = utcnow()
    db.session.commit()


# States that consume a runner's capacity. UNKNOWN is included
# deliberately — an uncertain execution may still have a live process;
# it must not silently free a slot until an operator resolves it.
_ACTIVE_STATES = [
    ExecutionState.DISPATCHED, ExecutionState.PREPARING,
    ExecutionState.RUNNING, ExecutionState.COLLECTING,
    ExecutionState.NORMALIZING, ExecutionState.CANCEL_REQUESTED,
    ExecutionState.UNKNOWN,
]


def _active_count_subquery(runner: Runner, exclude_id):
    return (db.session.query(db.func.count(Execution.id))
            .filter(Execution.runner_id == runner.id,
                    Execution.id != exclude_id,
                    Execution.state.in_(_ACTIVE_STATES))
            .scalar_subquery())


def _runner_cap(runner: Runner) -> int:
    return min(runner.max_slots,
               runner.license_slots or runner.max_slots)


def _assign_runner(ex: Execution, runner: Runner,
                   runtime: RuntimeConnection) -> bool:
    """Atomic QUEUED -> DISPATCHED assignment: runner/runtime/lease
    fields and the capacity check land in ONE conditional UPDATE, so
    two dispatchers can neither double-assign nor over-subscribe a
    runner's slots. Both the local path (then claims PREPARING) and
    the remote/agent path (agent picks up DISPATCHED) consume the slot
    here — capacity is reserved from assignment, not launch."""
    n = db.session.query(Execution).filter(
        Execution.id == ex.id,
        Execution.state == ExecutionState.QUEUED,
        _active_count_subquery(runner, ex.id) < _runner_cap(runner),
    ).update({"state": ExecutionState.DISPATCHED,
              "runner_id": runner.id,
              "runtime_id": runtime.id,
              "lease_expires_at": utcnow() + timedelta(
                  seconds=LEASE_SECONDS),
              "heartbeat_at": utcnow()},
             synchronize_session=False)
    db.session.commit()
    if n:
        db.session.refresh(ex)
    else:
        db.session.rollback()
    return bool(n)


def _claim_for_start(ex: Execution,
                     runner: Runner | None = None) -> bool:
    """Atomic QUEUED|DISPATCHED -> PREPARING claim — the precise launch
    boundary AND the capacity check, in one UPDATE. Below it the
    execution is cancel-scope; at/above it, started work runs (or takes
    CANCEL_REQUESTED). When a runner is given, the same conditional
    UPDATE also requires the runner's active count < its effective
    capacity — a slot is consumed atomically with the launch claim, so
    two dispatchers racing one remaining slot cannot both win."""
    q = db.session.query(Execution).filter(
        Execution.id == ex.id,
        Execution.state.in_([ExecutionState.QUEUED,
                             ExecutionState.DISPATCHED]))
    if runner is not None:
        q = q.filter(_active_count_subquery(runner, ex.id)
                     < _runner_cap(runner))
    n = q.update({"state": ExecutionState.PREPARING,
                  "heartbeat_at": utcnow()},
                 synchronize_session=False)
    db.session.commit()
    if n:
        db.session.refresh(ex)
    else:
        db.session.rollback()
    return bool(n)


def run_execution(execution_id: str) -> None:
    """Run one execution through the full state machine. Any infra error is
    recorded; test failures do not stop collection (§7)."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        return
    if (ex.snapshot().get("dispatch_transport") == "dagu-worker"
            and ex.state == ExecutionState.QUEUED):
        # Hard guarantee: a remote-dispatched execution must never enter
        # the local KRE path, whatever route it took here.
        _fail(ex, "infrastructure",
              "remote-dispatched execution reached the local execution "
              "path — refusing portal-host invocation")
        return
    # Dispatch boundary: only still-pending work may enter the machine —
    # an execution cancelled while its outbox event waited (user cancel
    # or plan stop-on-failure enforcement) is skipped, never launched.
    if ex.state not in (ExecutionState.QUEUED, ExecutionState.DISPATCHED):
        record_audit("system", "execution.dispatch_skipped",
                     f"{ex.id}: state={ex.state.value}")
        return
    # Explicit execution target (run_config.requested_runner/runtime):
    # re-validate eligibility HERE — drain/disable/mismatch may have
    # happened since the form was filled. An unavailable explicit pick
    # REJECTS visibly — never silently falls back to another target.
    rc = ex.snapshot().get("run_config") or {}
    st = ex.snapshot().get("source_target")
    if st:
        # The inventory row is replaceable per-SHA — re-validate the
        # durable triple, not the row id. A removed/renamed/type-changed
        # definition fails VISIBLY, never silently retargets.
        cur = DiscoveredItem.query.filter_by(
            binding_id=st["binding_id"], kind=st["kind"],
            path=st["path"]).first()
        if cur is None:
            _fail(ex, "config",
                  f"source definition {st['kind']} {st['path']!r} is "
                  "no longer in the binding's inventory — run rejected "
                  "(no retargeting)")
            return
    req_runner = rc.get("requested_runner") or ""
    req_runtime = rc.get("requested_runtime") or ""
    if req_runner:
        runner = db.session.get(Runner, req_runner)
        rn_allowed = (json.loads(runner.allowed_projects or "[]")
                      if runner else [])
        if (not runner or not runner.enabled or runner.draining
                or (rn_allowed and ex.project_id not in rn_allowed)):
            _fail(ex, "infrastructure",
                  f"explicitly selected runner {req_runner[:12]}… is "
                  "unavailable or out of scope for this project — no "
                  "fallback to another target")
            return
    else:
        runner = ex.runner or pick_runner(ex)
        if runner is None:
            # Stay QUEUED — retry the next worker tick; do NOT mark
            # DISPATCHED (the assignment CAS requires QUEUED).
            return
    if req_runtime:
        runtime = db.session.get(RuntimeConnection, req_runtime)
        rt_allowed = (json.loads(runtime.allowed_projects or "[]")
                      if runtime else [])
        if (not runtime or not runtime.enabled
                or runtime.runner_id != runner.id
                or (rt_allowed and ex.project_id not in rt_allowed)):
            _fail(ex, "infrastructure",
                  f"explicitly selected runtime {req_runtime[:12]}… is "
                  "unavailable or incompatible with the selected "
                  "runner — no fallback")
            return
    else:
        runtime = ex.runtime or pick_runtime(ex, runner)
        if runtime is None:
            _fail(ex, "infrastructure",
                  "no enabled compatible runtime connection on runner "
                  f"{runner.name!r} — register an existing KRE "
                  "installation")
            return
    if runner.connection_mode != "local":
        # Remote transport (§6): the assigned agent picks the task up via
        # /api/agent/tasks; the portal holds the lease, heartbeats renew
        # it, expiry reconciles to UNKNOWN — never silent re-dispatch.
        # Eligibility BEFORE the assignment CAS — a failed CAS just
        # leaves the execution queued rather than producing evidence.
        if not runner.agent_token_hash:
            _fail(ex, "infrastructure",
                  f"remote runner {runner.name!r} has no issued agent token")
            return
        if not _assign_runner(ex, runner, runtime):
            return   # lost race (cancelled or capacity) — stays queued
        return

    # Local path: consume the slot atomically, then claim PREPARING.
    if not _assign_runner(ex, runner, runtime):
        return   # lost race — another dispatcher owns it, or cancelled

    workspace_root = Path(
        runtime.workspace_root or runner.workspace_root
        or Path(current_app.instance_path) / "workspaces"
    )
    attempt_dir = workspace_root / execution_id
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"

    try:
        if not _claim_for_start(ex, runner):
            db.session.refresh(ex)
            if ex.state in (ExecutionState.CANCELED,
                            ExecutionState.CANCEL_REQUESTED):
                record_audit("system", "execution.dispatch_skipped",
                             f"{ex.id}: cancelled before launch boundary")
            # else: capacity won the race — stays queued for the next
            # tick; runner_id assignment is harmless until it starts.
            return
        attempt_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        source_dir = _resolve_source(ex)
        project_dir = kre_adapter.prepare_workspace(
            source_dir, attempt_dir, ex.source_sha
        )
        ex.workspace_path = str(attempt_dir)
        ex.report_dir = str(report_dir)
        ex.started_at = utcnow()

        rc = ex.snapshot().get("run_config") or {}
        # Version-gated options (§5): requested flags the detected KRE
        # build does not support are dropped with an evidence note —
        # never emitted as guessed flags.
        flags, dropped = kre_adapter.gated_flags(
            runtime.detected_version, rc.get("kre_options") or {})
        if dropped:
            # Explicit options the runtime cannot accept are REJECTED
            # before the process launches — the requested execution
            # semantics would change silently otherwise. Acknowledgement
            # (run_config.kre_options_allow_drop) is opt-in and audited.
            allow = bool(rc.get("kre_options_allow_drop"))
            log_dir.mkdir(parents=True, exist_ok=True)
            decision = ("drop (operator-acknowledged)" if allow
                        else "reject")
            note = (
                f"version-gated options for KRE "
                f"{runtime.detected_version or 'unknown'} — "
                f"dropped: {', '.join(dropped)}; requested: "
                f"{json.dumps(rc.get('kre_options') or {})}; "
                f"decision: {decision}")
            (log_dir / "kre-gating.log").write_text(note + "\n",
                                                  encoding="utf-8")
            record_audit(
                "system",
                "kre.options_gated" if allow else "kre.options_rejected",
                f"{ex.id}: {note}")
            if not allow:
                _fail(ex, "infrastructure",
                      f"KRE option gating rejected {len(dropped)} "
                      f"requested option(s) for detected version "
                      f"{runtime.detected_version or 'unknown'}: "
                      + ", ".join(dropped)
                      + " — launch aborted before dispatch; acknowledge "
                        "in run config to proceed without them")
                # The rejection record is registered evidence — _fail
                # skips _collect, so the gating log is filed here.
                gating_log = log_dir / "kre-gating.log"
                db.session.add(Artifact(
                    execution_id=ex.id, kind="log",
                    rel_path="logs/kre-gating.log",
                    sha256=results_mod.sha256_file(gating_log),
                    size=gating_log.stat().st_size,
                    mime="text/plain", mapped=False))
                db.session.commit()
                return
        ok_b, browser_val = kre_adapter.validate_browser(
            rc.get("browser", ""), runner.os_arch if runner else "")
        if not ok_b:
            _fail(ex, "config", browser_val)
            return
        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            project_path=str(project_dir),
            suite_path=_snapshot_suite_path(ex),
            report_dir=str(report_dir),
            profile=rc.get("profile", "default"),
            browser=browser_val or "Chrome (headless)",
            remote_webdriver_url=rc.get("remote_webdriver_url", ""),
            remote_webdriver_type=rc.get("remote_webdriver_type", ""),
            collection=bool(rc.get("collection")),
            extra=flags,
        )

        _transition(ex, ExecutionState.RUNNING)
        # Invocation credential (§5): the VALUE reaches the subprocess via
        # -apiKey= argv as KRE expects, but is scrubbed from every log/
        # artifact by run_invocation's redact pass.
        creds = _redact_values(runtime)
        # Remote WebDriver URL may embed user:pass@ — scrub the
        # userinfo portion from logs/audit/artifacts like any secret.
        rw_url = rc.get("remote_webdriver_url") or ""
        if "://" in rw_url and "@" in rw_url:
            creds.append(rw_url.split("@", 1)[0].split("://", 1)[1])
        if creds:
            args = args + [f"-apiKey={creds[0]}"]
        # Timeout: run_invocation's process-group kill is the same safe
        # path as cancel — a timed-out tree is never left running while
        # its slot reports free (wait/force-kill before release).
        result = kre_adapter.run_invocation(
            args, log_dir, cancel_event=_DbCancelEvent(ex.id),
            timeout_s=int(rc.get("timeout_seconds") or 3600),
            redact=creds,
        )
        ex.exit_status = result.exit_status
        ex.finished_at = utcnow()

        # Collection ALWAYS runs — including after failures (§5, §7).
        _transition(ex, ExecutionState.COLLECTING)
        _collect(ex, report_dir, log_dir)

        _transition(ex, ExecutionState.NORMALIZING)
        _normalize(ex)

        canceled = (result.canceled
                    or ex.state == ExecutionState.CANCEL_REQUESTED)
        if canceled:
            ex.outcome = Outcome.INCOMPLETE
            db.session.commit()
        else:
            error_kind = kre_adapter.classify_error(result)
            if error_kind:
                ex.error_kind = error_kind
                ex.error_text = f"KRE reported a {error_kind} error"
                db.session.commit()

        # Evidence export still runs for canceled executions —
        # partial evidence is real. Only the terminal state differs.
        _transition(ex, ExecutionState.FINALIZING)
        _sync_git(ex)

        if canceled:
            ex.state = ExecutionState.CANCELED
        else:
            ex.state = (ExecutionState.FAILED if ex.error_kind
                        else ExecutionState.COMPLETED)
        db.session.commit()
    except Exception as exc:  # infrastructure failure — record honestly
        _fail(ex, "infrastructure", f"{type(exc).__name__}: {exc}")
    finally:
        runner.heartbeat_at = utcnow()
        runner.health = "ok"
        db.session.commit()


def _redact_values(runtime: RuntimeConnection) -> list:
    """Resolve a runtime credential ref to VALUES for log scrubbing only —
    the secret itself never enters argv, artifacts, audit, or exports."""
    if not runtime or not runtime.credential_ref:
        return []
    from .secrets_store import SecretError, read_secret
    try:
        return [read_secret(current_app._get_current_object(),
                            runtime.credential_ref)]
    except SecretError:
        return []


def _fail(ex: Execution, kind: str, text: str) -> None:
    ex.state = ExecutionState.FAILED
    ex.error_kind = kind
    ex.error_text = text[:2000]
    ex.finished_at = utcnow()
    ex.outcome = Outcome.INCOMPLETE
    db.session.commit()


def _resolve_source(ex: Execution) -> Path:
    """Source contents for the isolated workspace: the binding's synced
    worktree (recorded SHA), never project code executed at index time."""
    if ex.binding_id:
        binding = db.session.get(GitBinding, ex.binding_id)
        if binding and binding.worktree_path:
            base = Path(binding.worktree_path)
            if binding.subdirectory:
                base = base / binding.subdirectory
            return base
    return Path(ex.workspace_path or "/nonexistent")


def _snapshot_suite_path(ex: Execution) -> str:
    # An explicitly selected discovered suite/collection path takes
    # precedence; otherwise the first entry's case_ref (existing
    # suite-list semantics).
    rc = ex.snapshot().get("run_config") or {}
    if rc.get("suite_path"):
        return rc["suite_path"]
    entries = [e for e in ex.snapshot().get("entries", []) if e.get("enabled")]
    first = entries[0]["case_ref"] if entries else ""
    return first or "Test Suites/suite"


def _collect(ex: Execution, report_dir: Path, log_dir: Path) -> None:
    """Evidence collection: JUnit discovery + manifest with checksums;
    every file is registered, unmapped files stay at run level (§9)."""
    junit_files = set(kre_adapter.discover_reports(report_dir))
    manifest = results_mod.build_evidence_manifest(report_dir, junit_files)
    # Runner logs are evidence too — including the recorded argv.
    for log_path in sorted(log_dir.glob("kre-*.log")):
        manifest.append({
            "rel_path": f"logs/{log_path.name}",
            "kind": "log",
            "sha256": results_mod.sha256_file(log_path),
            "size": log_path.stat().st_size,
            "mime": "text/plain",
            "active_content": False,
        })
    for item in manifest:
        db.session.add(Artifact(
            execution_id=ex.id,
            kind=item["kind"], rel_path=item["rel_path"],
            sha256=item["sha256"], size=item["size"], mime=item["mime"],
            mapped=item["kind"] == "junit",
        ))
    db.session.commit()
    # Local-report status is tracked separately from state/outcome.
    try:
        from . import reporting
        reporting.refresh_report_status(ex)
        # Durable publication workflow — queued once the report is
        # finalized; failures stay separate from the test outcome.
        if ex.report_pub_status == "none" and not ex.deletion_status:
            ex.report_pub_status = "pending"
            db.session.add(OutboxEvent(
                kind="report-publish",
                payload_json=json.dumps(
                    {"execution_id": ex.id})))
            db.session.commit()
    except Exception as exc:
        try:
            reporting.refresh_failed(ex, exc)
        except Exception:
            pass  # reporting must never break the execution pipeline


def _normalize(ex: Execution) -> None:
    """Canonical result rows from actual JUnit output. Discrepant producer
    totals and unmapped cases are surfaced, not guessed away (§8)."""
    junit_artifacts = Artifact.query.filter_by(
        execution_id=ex.id, kind="junit").all()
    cases = []
    seen_names = {}
    for artifact in junit_artifacts:
        path = Path(ex.report_dir) / artifact.rel_path
        try:
            parsed = parse_junit(path)
        except Exception:
            continue
        for case in parsed.cases:
            key = (case.classname, case.name)
            seen_names[key] = seen_names.get(key, 0) + 1
            cases.append((case, seen_names[key], parsed))
    completed = ex.exit_status is not None
    for case, occurrence, parsed in cases:
        db.session.add(TestResult(
            execution_id=ex.id,
            case_ref=case.classname or case.name,
            case_name=case.name,
            classname=case.classname,
            iteration=case.iteration,
            attempt=occurrence,
            status=case.status,
            duration_s=case.duration_s,
            message=case.message[:4000],
            unresolved=parsed.discrepant,
        ))
    ex.outcome = Outcome(
        aggregate_outcome([c[0] for c in cases], completed=completed)
    )
    db.session.commit()


def _sync_git(ex: Execution) -> None:
    """Git export outbox (§11). Sync state is independent of outcome."""
    from . import gitsync
    try:
        gitsync.export_execution(ex)
        ex.git_state = SyncState.DONE
    except gitsync.NotConfigured:
        ex.git_state = SyncState.NOT_CONFIGURED
    except Exception as exc:
        ex.git_state = SyncState.FAILED
        ex.error_text = (ex.error_text + f" | git: {exc}")[:2000]
    db.session.commit()


# ---------- outbox processing ----------

def process_outbox(limit: int = 10) -> int:
    """Claim and process pending durable events. Status transitions are
    committed before side effects; failures leave a retryable record with
    the error — an ambiguous timeout is reconciled, never blind-duplicated."""
    now = utcnow()
    events = OutboxEvent.query.filter(
        OutboxEvent.status == "pending",
        OutboxEvent.available_at <= now,
    ).order_by(OutboxEvent.created_at).limit(limit).all()
    processed = 0
    for event in events:
        event.status = "processing"
        event.attempts += 1
        db.session.commit()
        try:
            _handle_event(event)
            event.status = "done"
            event.processed_at = utcnow()
        except Exception as exc:
            event.status = "pending" if event.attempts < 5 else "failed"
            event.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            event.available_at = utcnow() + timedelta(
                seconds=min(300, 2 ** event.attempts))
        db.session.commit()
        processed += 1
    return processed


def _handle_event(event: OutboxEvent) -> None:
    payload = event.payload()
    if event.kind == "report-publish":
        from .gitsync import publish_report
        publish_report(payload["execution_id"])
        return
    if event.kind == "report-delete":
        from .gitsync import delete_report
        delete_report(payload["execution_id"])
        return
    if event.kind in ("local-dispatch", "dagu-dispatch"):
        if event.kind == "dagu-dispatch":
            # Owner-approved Option B: Dagu is the MANDATORY orchestrator.
            # Submission creates the workflow-run identity FIRST; the
            # worker then executes under that correlation. A missing or
            # unreachable Dagu fails the dispatch honestly — no silent
            # self-orchestration, no blind resubmission on ambiguity.
            ex = db.session.get(Execution, payload["execution_id"])
            try:
                if (ex is not None
                        and ex.snapshot().get("dispatch_transport")
                        == "dagu-worker"):
                    _submit_remote(ex)
                    return
                dispatch_id = DaguAdapter.from_settings().submit(payload)
                if ex:
                    ex.dispatch_id = dispatch_id
                    db.session.commit()
                    # OWNERSHIP (Option B): Dagu now owns the run; the
                    # portal assigns the authorized runner/runtime up
                    # front (capability/project/capacity checked) so the
                    # workflow's agent picks up exactly this task.
                    if _dagu_mandatory():
                        runner = pick_runner(ex)
                        runtime = (ex.runtime
                                   or (pick_runtime(ex, runner)
                                       if runner else None))
                        if not runner or not runtime:
                            _fail(ex, "infrastructure",
                                  "no eligible runner/runtime for "
                                  "dagu-coordinated dispatch")
                            return
                        if not _assign_runner(ex, runner, runtime):
                            return   # lost race — stays queued, next
                        return       # tick retries
            except Exception as exc:
                if _dagu_mandatory():
                    if ex:
                        _fail(ex, "infrastructure",
                              f"Dagu orchestrator unreachable/not "
                              f"configured — reconcile before retry: "
                              f"{str(exc)[:300]}")
                    return
                # Non-mandatory dev/test mode: fall back to local path.
        run_execution(payload["execution_id"])
    elif event.kind == "git-sync":
        from . import gitsync
        gitsync.sync_binding(payload["binding_id"])
    elif event.kind == "rp-publish":
        pass  # removed integration — absorb stale queued events harmlessly
    elif event.kind == "runner-heartbeat":
        runner = db.session.get(Runner, payload["runner_id"])
        if runner:
            runner.heartbeat_at = utcnow()
            runner.health = "ok"
            db.session.commit()
    else:
        raise DispatchError(f"unknown outbox kind {event.kind!r}")


def reconcile_stale() -> int:
    """Restart reconciliation (A05/A06): executions whose lease/heartbeat
    expired while non-terminal become UNKNOWN — never re-dispatched."""
    now = utcnow()
    stale = Execution.query.filter(
        Execution.state.in_([
            ExecutionState.DISPATCHED, ExecutionState.PREPARING,
            ExecutionState.RUNNING, ExecutionState.COLLECTING,
            ExecutionState.NORMALIZING, ExecutionState.CANCEL_REQUESTED,
        ]),
        Execution.lease_expires_at.isnot(None),
        Execution.lease_expires_at < now,
    ).all()
    for ex in stale:
        ex.state = ExecutionState.UNKNOWN
        ex.error_kind = "infrastructure"
        # Durable audit trail for ambiguous resolution (§18): every
        # UNKNOWN is attributable, never silent or auto-rerun.
        record_audit("system", "execution.reconciled_unknown", ex.id)
        ex.error_text = (
            ex.error_text + " | lease expired; state uncertain "
            "(reconciled, not re-run)").strip(" |")[:2000]
        db.session.commit()
    return len(stale)


# ---------- schedules & plan policies (§10) ----------

def fire_due_schedules(now=None) -> int:
    """Fire enabled schedules whose next_run_at has passed — the worker
    tick owns firing; request handlers only manage definitions. A missed
    fire time runs late on the next tick and is audited, never dropped."""
    now = now or utcnow()
    due = Schedule.query.filter(
        Schedule.enabled.is_(True),
        Schedule.next_run_at.isnot(None),
        Schedule.next_run_at <= now).all()
    fired = 0
    for sched in due:
        # Atomic claim (multi-worker safe): exactly one worker advances
        # next_run_at/disables for THIS due time — the loser sees
        # rowcount 0 and skips. A fire failure rolls the claim back so
        # the schedule stays due; crash-after-claim is audited, and a
        # 'once' schedule can never double-dispatch.
        try:
            if sched.kind == "once":
                claimed = db.session.query(Schedule).filter(
                    Schedule.id == sched.id,
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at == sched.next_run_at,
                ).update({"enabled": False, "next_run_at": None},
                         synchronize_session=False)
            else:
                claimed = db.session.query(Schedule).filter(
                    Schedule.id == sched.id,
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at == sched.next_run_at,
                ).update({"next_run_at": now + timedelta(
                             minutes=sched.interval_minutes or 60)},
                         synchronize_session=False)
        except OperationalError as exc:
            # Only expected writer-lock/busy conditions are retried —
            # unrelated DB failures stay visible (raised, not skipped).
            if "locked" not in str(exc).lower() \
                    and "busy" not in str(exc).lower():
                raise
            db.session.rollback()   # session stays usable next tick
            continue
        if not claimed:
            db.session.rollback()
            continue  # another worker claimed this fire
        try:
            # Post-claim refresh: fire the row's CURRENT config/target —
            # a concurrent edit/disable/delete between the due scan and
            # the claim must not dispatch stale values. A deleted row
            # raises here and stays unfired.
            db.session.refresh(sched)
            cfg = sched.run_config()
            # Overlap policy (interval schedules only — 'once' fires a
            # single time): skip = drop this occurrence while a previous
            # execution from THIS schedule is still active. Evaluated
            # inside the already-claimed row's transaction — the claim
            # already advanced next_run_at, so the skipped slot moves
            # forward (no catch-up storm). UNKNOWN counts as active —
            # uncertain work blocks overlap until resolved.
            if sched.kind == "interval" and                     sched.overlap_policy == "skip":
                active = db.session.query(Execution).filter(
                    Execution.schedule_id == sched.id,
                    Execution.state.in_(_ACTIVE_STATES)).count()
                if active:
                    record_audit("system", "schedule.overlap_skipped",
                                 f"{sched.id}: {active} active")
                    db.session.commit()
                    continue
            binding = GitBinding.query.filter_by(
                project_id=sched.project_id).first()
            sha = binding.last_synced_sha if binding else ""
            targets = sched.targets()
            if targets:
                for tgt in targets:
                    d_item = db.session.get(
                        DiscoveredItem, tgt.get("source_item_id"))
                    if (d_item is None or
                            d_item.binding.project_id
                            != sched.project_id):
                        record_audit("worker", "schedule.target_unavailable",
                                     sched.id)
                        continue
                    create_execution(sched.created_by, None,
                                     binding=binding,
                                     source_sha=sha, run_config=cfg,
                                     schedule_id=sched.id,
                                     source_item=d_item)
                continue
            if sched.target_type == "plan" and sched.plan_id:
                plan = db.session.get(Plan, sched.plan_id)
                if plan is None:
                    raise DispatchError("scheduled plan no longer exists")
                queue_plan_executions(sched.created_by, plan,
                                      binding=binding, source_sha=sha,
                                      schedule_id=sched.id,
                                      run_config=cfg)
            elif sched.target_type == "suite" and sched.suite_id:
                suite = db.session.get(Suite, sched.suite_id)
                if suite is None:
                    raise DispatchError("scheduled suite no longer exists")
                create_execution(sched.created_by, suite, binding=binding,
                                 source_sha=sha, run_config=cfg,
                                 schedule_id=sched.id)
            else:
                raise DispatchError(
                    f"invalid schedule target {sched.target_type!r}")
            record_audit(sched.created_by, "schedule.fired", sched.id)
            fired += 1
        except Exception as exc:
            db.session.rollback()   # un-claim — stays due, fire retried
            record_audit("system", "schedule.fire_failed",
                         f"{sched.id}: {type(exc).__name__}: {exc}")
            db.session.commit()
        else:
            sched.last_run_at = now
            db.session.commit()     # claim + execution commit together
    return fired


def enforce_plan_policies() -> int:
    """Per-entry failure policy (§10): an entry configured
    on_failure=stop whose LATEST execution reached a terminal non-passed
    state cancels its plan-run siblings still queued/dispatched —
    entries already running finish naturally (cancel-queued scope only).

    Runs every tick, restart-safe: a missed enforcement simply applies
    late. A queued retry ('not-started' latest state) holds enforcement."""
    cancelled = 0
    runs = {}
    # Every execution of a run matters — including pending retries:
    # collecting only terminal rows would let a queued retry be ignored
    # and siblings wrongly cancelled.
    for ex in Execution.query.all():
        rid = ex.snapshot().get("plan_run_id")
        if rid:
            runs.setdefault(rid, []).append(ex)
    if not runs:
        return 0
    pending = {e.id: e for e in Execution.query.filter(
        Execution.state.in_([ExecutionState.QUEUED,
                             ExecutionState.DISPATCHED])).all()}
    for rid, exs in runs.items():
        plan = db.session.get(
            Plan, exs[0].snapshot().get("plan_id") or "")
        if plan is None:
            continue
        entries = {e.suite_id: e for e in plan.entries}
        # Latest execution per entry decides — a retry in flight blocks.
        latest = {}
        for ex in exs:
            sid = ex.snapshot().get("suite_id")
            if sid not in latest or (
                    ex.created_at and latest[sid].created_at
                    and ex.created_at > latest[sid].created_at):
                latest[sid] = ex
        stop_orders = []
        for sid, ex in latest.items():
            entry = entries.get(sid)
            if entry is None:
                continue
            cfg = json.loads(entry.config_json or "{}")
            if cfg.get("on_failure") == "stop" \
                    and entry_state(ex) not in ("passed", "not-started"):
                stop_orders.append(entry.order)
        if not stop_orders:
            continue
        threshold = min(stop_orders)
        for sib in pending.values():
            snap = sib.snapshot()
            if snap.get("plan_run_id") != rid:
                continue
            entry = entries.get(snap.get("suite_id"))
            if entry is None or entry.order <= threshold:
                continue
            # Atomic cancel-scope transition: only if the sibling is
            # STILL queued/dispatched. If its launch claim landed first
            # (PREPARING+), this UPDATE matches 0 rows — started work
            # continues; cancel scope is defined at the DB row, not on
            # a stale ORM snapshot.
            n = db.session.query(Execution).filter(
                Execution.id == sib.id,
                Execution.state.in_([ExecutionState.QUEUED,
                                     ExecutionState.DISPATCHED]),
            ).update({
                "state": ExecutionState.CANCELED,
                "cancel_reason": (
                    f"plan stop-on-failure: entry order {threshold} "
                    "ended non-passed"),
                "outcome": Outcome.INCOMPLETE,
                "finished_at": utcnow(),
            }, synchronize_session=False)
            if not n:
                continue   # already started — out of cancel scope
            if sib.dispatch_id:
                try:
                    DaguAdapter.from_settings().cancel(sib.dispatch_id)
                except Exception:
                    pass
            record_audit("system", "plan.stop_on_failure_cancel", sib.id)
            cancelled += 1
    if cancelled:
        db.session.commit()
    return cancelled


def worker_once() -> int:
    n = (reconcile_stale() + fire_due_schedules() + process_outbox()
         + enforce_plan_policies())
    # Dagu-side reconciliation (§7): a finished dagu run with no portal
    # result is ambiguous -> UNKNOWN, never a blind re-dispatch. Agent
    # heartbeats — not dagu run state — own lease renewal.
    if _dagu_configured():
        try:
            n += reconcile_dagu_runs()
        except Exception:
            pass  # orchestrator unreachable — retry next cycle
    return n


def worker_loop(interval: float = 2.0) -> None:
    while True:
        worker_once()
        time.sleep(interval)


# ---------- Dagu-worker remote dispatch ----------

def _submit_remote(ex: Execution) -> None:
    """Dagu-worker remote dispatch: an ad-hoc spec enqueued with the
    execution-pinned dagRunId and the installation's unique worker
    label. The worker checks out the recorded source revision itself,
    runs the registered executable, and reports status/logs/artifacts
    to the coordinator — nothing KRE-side runs on the portal host."""
    snap = ex.snapshot()
    rc = snap.get("run_config") or {}
    rt = db.session.get(RuntimeConnection,
                        rc.get("requested_runtime") or ex.runtime_id)
    if (rt is None or not rt.enabled or not rt.worker_label
            or not rt.executable_path):
        _fail(ex, "infrastructure",
              "worker-bound runtime is unavailable, unlabeled, or has "
              "no executable path — no local fallback")
        return
    binding = (db.session.get(GitBinding, ex.binding_id)
               if ex.binding_id else None)
    if binding is None or not binding.url:
        _fail(ex, "config",
              "remote dispatch requires a git-bound source — the "
              "worker checks the revision out itself")
        return
    try:
        adapter = DaguAdapter.from_settings()
        workers = adapter.remote_worker_names("kre", rt.worker_label)
    except Exception as exc:
        _fail(ex, "infrastructure",
              f"Dagu coordinator unreachable/not configured — "
              f"reconcile before retry: {str(exc)[:300]}")
        return
    if len(workers) != 1:
        _fail(ex, "infrastructure",
              (f"no connected Dagu worker carries kre="
               f"{rt.worker_label} (bounded precheck — nothing was "
               "dispatched)" if not workers else
               f"ambiguous match: {len(workers)} workers carry kre="
               f"{rt.worker_label} — installation labels must be "
               "unique"))
        return

    workspace = (rt.workspace_root or "/var/lib/to-worker").rstrip("/") \
        + "/" + ex.id
    flags, dropped = kre_adapter.gated_flags(
        rt.detected_version, rc.get("kre_options") or {})
    if dropped and not rc.get("kre_options_allow_drop"):
        _fail(ex, "infrastructure",
              f"version-gated options rejected for detected version "
              f"{rt.detected_version or 'unknown'}: "
              + ", ".join(dropped))
        return
    ok_b, browser_val = kre_adapter.validate_browser(
        rc.get("browser", ""), "linux")
    if not ok_b:
        _fail(ex, "config", browser_val)
        return
    # -apiKey is intentionally NOT sent: remote KRE uses the licence
    # installed on the worker host — an activation credential must not
    # be embedded in a spec stored by the coordinator.
    kre_args = kre_adapter.build_args(
        executable=rt.executable_path,
        project_path=workspace + "/proj",
        suite_path=_snapshot_suite_path(ex),
        report_dir="${context.paths.artifacts_dir}/Reports",
        profile=rc.get("profile", "default"),
        browser=browser_val or "Chrome (headless)",
        remote_webdriver_url=rc.get("remote_webdriver_url", ""),
        remote_webdriver_type=rc.get("remote_webdriver_type", ""),
        collection=bool(rc.get("collection")),
        extra=flags,
    )
    git_auth = {}
    url = str(binding.url)
    if url.startswith(("ssh://", "git@")):
        # Worker-side ssh: the key path names a file on the WORKER —
        # key material never crosses the wire. Empty auth lets the
        # worker's own ssh-agent answer (documented scope: the worker
        # account's ssh keys + host-key policy are operator-managed).
        if rc.get("git_ssh_key_path"):
            git_auth["ssh_key_path"] = rc["git_ssh_key_path"]
        if rc.get("git_ssh_passphrase"):
            git_auth["ssh_passphrase"] = rc["git_ssh_passphrase"]
    elif binding.credential_ref:
        # Credential boundary: a submitted spec is stored by the
        # coordinator and readable via its API/UI — embedding an HTTPS
        # password/token would expose it. Restricted until Dagu
        # server-side secrets are provisioned: remote dispatch accepts
        # unauthenticated remotes (file://, public https) and
        # worker-side ssh keys only.
        _fail(ex, "infrastructure",
              "remote dispatch cannot embed the binding's HTTPS "
              "credential in a stored workflow spec — use an "
              "unauthenticated remote or worker-side ssh_key_path "
              "(see docs/integration-contracts.md)")
        return

    spec = adapter.remote_kre_spec(
        worker_label=rt.worker_label,
        repository=binding.url,
        ref=ex.source_sha or binding.ref or "main",
        git_auth=git_auth,
        remote_workspace=workspace,
        executable=rt.executable_path,
        args=kre_args[1:],
        timeout_s=int(rc.get("timeout_seconds") or 3600))
    import json as _json
    try:
        dag_run_id = adapter.enqueue(
            spec_yaml=_json.dumps(spec),
            name=DaguAdapter.REMOTE_DAG_NAME,
            dag_run_id=ex.id,
            worker_selector={"kre": rt.worker_label},
            params=f"execution_id={ex.id}",
            singleton=True)
    except Exception as exc:
        # 409 = a run with this id already exists — adopt its identity;
        # a retry of this outbox event can never duplicate the run.
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code == 409:
            dag_run_id = ex.id
        else:
            _fail(ex, "infrastructure",
                  f"Dagu submission failed — reconcile before retry: "
                  f"{str(exc)[:300]}")
            return
    ex.dispatch_id = dag_run_id
    ex.runner_id = rt.runner_id
    ex.runtime_id = rt.id
    ex.state = ExecutionState.DISPATCHED
    ex.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
    db.session.commit()
    record_audit("system", "execution.remote_dispatched",
                 f"{ex.id}: dagu worker kre={rt.worker_label} "
                 f"run={dag_run_id}")


def _artifact_files(items: list, prefix: str = "") -> list:
    """Flatten the recursive artifact tree to file paths."""
    out = []
    for item in items or []:
        path = item.get("path") or ""
        if item.get("type") == "file":
            out.append(path)
        out.extend(_artifact_files(item.get("children") or []))
    return out


def _collect_remote(adapter: DaguAdapter, ex: Execution,
                    label: str) -> None:
    """Terminal dagu run -> materialize coordinator artifacts into the
    portal collection dir and run the SAME collect/normalize/finalize
    pipeline as a local execution — partial downloads are honest
    evidence, never guessed."""
    attempt_dir = (Path(current_app.instance_path)
                   / "remote-runs" / ex.id)
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        for rel in _artifact_files(adapter.list_artifacts(
                DaguAdapter.REMOTE_DAG_NAME, ex.dispatch_id)):
            dest = attempt_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(adapter.download_artifact(
                DaguAdapter.REMOTE_DAG_NAME, ex.dispatch_id, rel))
    except Exception as exc:
        _fail(ex, "infrastructure",
              f"remote run {ex.dispatch_id} ended '{label}' but "
              f"artifact retrieval failed: {exc}")
        return
    try:
        (log_dir / "kre-remote-run.log").write_bytes(
            adapter.run_log(DaguAdapter.REMOTE_DAG_NAME, ex.dispatch_id)
            .encode("utf-8", "replace"))
    except Exception:
        pass  # run log is best-effort evidence
    ex.workspace_path = str(attempt_dir)
    ex.report_dir = str(report_dir)
    ex.exit_status = 0 if label == "succeeded" else 1
    ex.finished_at = utcnow()

    _transition(ex, ExecutionState.COLLECTING)
    _collect(ex, report_dir, log_dir)
    _transition(ex, ExecutionState.NORMALIZING)
    _normalize(ex)
    _transition(ex, ExecutionState.FINALIZING)
    _sync_git(ex)
    has_results = bool(TestResult.query.filter_by(
        execution_id=ex.id).first())
    if label in ("aborted", "canceled"):
        if ex.state == ExecutionState.CANCEL_REQUESTED \
                or ex.cancel_reason:
            # User-requested termination: CANCELED is the true cause.
            ex.state = ExecutionState.CANCELED
            ex.outcome = Outcome.INCOMPLETE
        else:
            # An abort we did not request is NOT a user cancellation —
            # worker shutdown/loss or an external stop are all possible
            # and Dagu's status cannot distinguish them. Preserve the
            # uncertainty honestly instead of misclassifying cause.
            ex.state = ExecutionState.FAILED
            ex.error_kind = "infrastructure"
            ex.error_text = (
                f"remote run {ex.dispatch_id} ended '{label}' without "
                "a recorded cancellation request — cause undetermined "
                "(worker shutdown/loss or external stop); any late "
                "evidence was collected")
            ex.outcome = Outcome.INCOMPLETE
            record_audit("system", "execution.remote_aborted_unrequested",
                         ex.id)
    elif label == "succeeded" or has_results:

        # A 'failed' dagu run with real junit is failed TESTS — the
        # normalized outcome carries the verdict, not the step status.
        ex.state = ExecutionState.COMPLETED
    else:
        ex.state = ExecutionState.FAILED
        ex.error_kind = "execution"
        ex.error_text = (f"remote run {ex.dispatch_id} ended "
                         f"'{label}' with no result data")
    db.session.commit()
    # Workspace retention: evidence is now materialized portal-side —
    # request bounded worker-side cleanup for this execution's dir.
    rt = db.session.get(RuntimeConnection, ex.runtime_id)
    if rt is not None and rt.worker_label:
        _request_ws_cleanup(adapter, ex, rt)


WS_CLEANUP_DAG = "to-ws-cleanup"


def _ws_cleanup_marker(ex: Execution) -> Path:
    return (Path(current_app.instance_path) / "remote-runs" / ex.id
            / ".ws-cleanup")


def _request_ws_cleanup(adapter: DaguAdapter, ex: Execution,
                        rt: RuntimeConnection) -> None:
    """Enqueue a bounded cleanup run for ONE execution workspace —
    deletion is restricted to <workspace_root>/<exec-id>, a path this
    process generated, never an arbitrary worker directory. The marker
    file makes retries idempotent; a queued run that outlives the
    worker's wait timeout fails and is surfaced by the sweep."""
    marker = _ws_cleanup_marker(ex)
    if marker.exists():
        return
    root = (rt.workspace_root or "").rstrip("/")
    if not root or root == "/" or not ex.id.isalnum():
        return  # never compose a deletion out of unsafe parts
    ws = f"{root}/{ex.id}"
    spec = {"worker_selector": {"kre": rt.worker_label},
            "timeout_sec": 300,
            "steps": [{"id": "rm",
                       "run": f"test -d {ws} && rm -rf -- {ws}"}]}
    try:
        adapter.enqueue(spec_yaml=json.dumps(spec),
                        name=WS_CLEANUP_DAG,
                        dag_run_id=f"cleanup-{ex.id}",
                        worker_selector={"kre": rt.worker_label},
                        singleton=True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pending")
        record_audit("system", "execution.remote_ws_cleanup_requested",
                     ex.id)
    except Exception as exc:
        ex.error_text = (ex.error_text +
                         f" | remote workspace cleanup request "
                         f"failed: {str(exc)[:200]}")[:2000]
        db.session.commit()


def _sweep_ws_cleanup(adapter: DaguAdapter,
                      retention_hours: int = 24) -> int:
    """Follow up on enqueued cleanup runs and request cleanup for
    eligible terminal executions. Bounded: only terminal-state remote
    executions, only their own <root>/<id> dir, only after portal-side
    evidence exists or the retention deadline passed. Cleanup failures
    are recorded, not ignored."""
    n = 0
    for ex in Execution.query.filter(Execution.state.in_([
            ExecutionState.COMPLETED, ExecutionState.FAILED,
            ExecutionState.CANCELED, ExecutionState.UNKNOWN])).all():
        if ex.snapshot().get("dispatch_transport") != "dagu-worker":
            continue
        marker = _ws_cleanup_marker(ex)
        if marker.exists():
            if marker.read_text().strip() != "pending":
                continue
            try:
                remote = adapter.dag_run(WS_CLEANUP_DAG,
                                         f"cleanup-{ex.id}")
            except Exception:
                continue
            d2 = remote.get("dagRunDetails") or remote.get("dagRun") or {}
            lbl = d2.get("statusLabel") or ""
            if lbl == "succeeded":
                marker.write_text("cleaned")
            elif lbl in ("failed", "aborted", "error", "canceled"):
                marker.write_text("failed")
                ex.error_text = (
                    ex.error_text +
                    f" | remote workspace cleanup failed ({lbl}) — "
                    "manual removal may be needed")[:2000]
                db.session.commit()
                record_audit("system",
                             "execution.remote_ws_cleanup_failed", ex.id)
                n += 1
            continue
        has_evidence = bool(Artifact.query.filter_by(
            execution_id=ex.id).first()) \
            or ex.state == ExecutionState.COMPLETED
        stale = (ex.finished_at is not None
                 and ex.finished_at < utcnow()
                 - timedelta(hours=retention_hours))
        if not (has_evidence or stale):
            continue
        rt = db.session.get(RuntimeConnection, ex.runtime_id)
        if rt is None or not rt.worker_label:
            continue
        _request_ws_cleanup(adapter, ex, rt)
        n += 1
    return n


def _poll_remote_run(adapter: DaguAdapter, ex: Execution) -> int:
    """Remote-transport runs: the dagu run IS the execution. Alive runs
    renew the lease (the coordinator is the aliveness source); terminal
    runs trigger collection — 'queued' with no worker is bounded by the
    coordinator's worker_wait_timeout."""
    try:
        remote = adapter.dag_run(DaguAdapter.REMOTE_DAG_NAME,
                                 ex.dispatch_id)
    except Exception:
        return 0   # transient — lease expiry bounds the ambiguity
    d = remote.get("dagRunDetails") or remote.get("dagRun") or {}
    label = d.get("statusLabel") or ""
    if label in ("succeeded", "failed", "aborted", "error", "canceled"):
        _collect_remote(adapter, ex, label)
        return 1
    if label in ("queued", "running", "not_started", "created"):
        ex.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
        db.session.commit()
    return 0


def reconcile_dagu_runs() -> int:
    """Poll DISPATCHED executions' dagu runs (§7 restart/reconciliation):
    finished-without-result -> UNKNOWN (never silently re-run); running
    leases get extended only while the run is actually alive."""
    adapter = None
    fixed = 0
    for ex in Execution.query.filter(
            Execution.state.in_([ExecutionState.DISPATCHED,
                                 ExecutionState.CANCEL_REQUESTED])).all():
        if not ex.dispatch_id:
            continue
        adapter = adapter or DaguAdapter.from_settings()
        if ex.snapshot().get("dispatch_transport") == "dagu-worker":
            fixed += _poll_remote_run(adapter, ex)
            continue
        remote = adapter.status(ex.dispatch_id)
        label = (remote.get("statusLabel") or remote.get("status"))
        if label in ("succeeded", "failed", "aborted", "error"):
            # Dagu finished but the portal has no result -> ambiguous:
            # reconcile to UNKNOWN, not a re-dispatch.
            ex.state = ExecutionState.UNKNOWN
            ex.error_kind = "infrastructure"
            ex.error_text = f"dagu run {ex.dispatch_id} ended '{label}' " \
                            "without portal result"
            fixed += 1
        # NOTE: a "running" dagu run does NOT extend the execution lease —
        # the monitor step keeps the dagu run alive while the remote
        # agent may be dead. Agent heartbeats own lease renewal; expiry
        # still reconciles to UNKNOWN via reconcile_stale.
    if adapter is not None:
        try:
            _sweep_ws_cleanup(adapter)
        except Exception:
            pass  # cleanup sweep must not break reconciliation
    db.session.commit()
    return fixed
