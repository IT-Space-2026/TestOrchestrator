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

import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app

from . import settings as settings_mod
from .adapters import kre as kre_adapter
from .adapters import nodefw, robotfw


def _tag_list(raw) -> list:
    """Comma-separated tag list -> sanitized tokens (no empty items,
    no shell metacharacters — argv only, never a shell)."""
    out = []
    for t in str(raw or "").split(","):
        t = t.strip()
        if t and all(c.isalnum() or c in "-_:. " for c in t):
            out.append(t[:64])
    return out
from .adapters.buildbot import BuildbotAdapter
from . import buildbot_cfg
from .execution import (Artifact, DiscoveredItem, Execution,
                        ExecutionState, GitBinding,
                        OutboxEvent, Outcome, Plan, Release, Runner,
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

def _snapshot_runtime_kind(rc: dict) -> str:
    """Runtime kind for the run_config's selected runtime — decides
    whether the recorded browser default is KRE's or the framework's.
    Unknown/unresolved runtime records KRE (the pre-2.6 default)."""
    rid = str(rc.get("runtime_id") or "").strip()
    if not rid:
        return "kre"
    rt = db.session.get(RuntimeConnection, rid)
    return (getattr(rt, "runtime_kind", "") or "kre") if rt else "kre"


def create_execution(username: str, suite: Suite = None,
                     binding: GitBinding = None,
                     plan_id: str = "",
                     release_id: str = None,
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

    project_id = (suite.project_id if suite else source_item.project_id)
    if release_id:
        rel = db.session.get(Release, release_id)
        if rel is None or rel.project_id != project_id:
            raise DispatchError(
                "selected release is unavailable or out of scope for "
                "this project")

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
            # KRE default applies to KRE runtimes only — node runtimes
            # record the framework browser ("" = framework default).
            "browser": (str(rc.get("browser")
                            or "Chrome (headless)")[:128]
                        if _snapshot_runtime_kind(rc) == "kre"
                        else str(rc.get("browser") or "")[:128]),
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
            # Robot Framework selection knobs (kind=robot only —
            # ignored by other runtime kinds).
            "robot_suite": str(rc.get("robot_suite") or "")[:256],
            "robot_test": str(rc.get("robot_test") or "")[:256],
            "robot_include": str(rc.get("robot_include") or "")[:512],
            "robot_exclude": str(rc.get("robot_exclude") or "")[:512],
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
        # worker_label runtimes are bindings to a removed worker
        # transport. They fail VISIBLY here rather than silently
        # rerouting: the runtime must be re-registered against a
        # Buildbot target.
        if _rt.worker_label:
            raise DispatchError(
                "remote execution is not configured/available — "
                "this installation is bound to a removed worker "
                "transport; re-register it against a Buildbot target")
        # Buildbot targets: the runner's target_type picks the
        # orchestrated transport.
        elif _buildbot_configured() and _rt.runner is not None \
                and _rt.runner.target_type in (
                    "local", "buildbot-worker", "ssh"):
            snapshot["dispatch_transport"] = "buildbot"
    snapshot.update(extra_snapshot or {})
    execution = Execution(
        project_id=project_id,
        suite_id=suite.id if suite else None,
        release_id=release_id or None,
        source_item_id=source_item.id if source_item else None,
        binding_id=(binding.id if binding else
                    (source_item.binding_id if source_item else None)),
        snapshot_json=json.dumps(snapshot),
        source_sha=source_sha,
        # source_ref = the SELECTED ref (selector), distinct from the
        # resolved source_sha. environment = effective run_config value.
        source_ref=((binding.ref or None) if binding else None),
        environment=(rc.get("environment") or None),
        created_by=username,
        schedule_id=schedule_id or None,
    )
    db.session.add(execution)
    db.session.flush()  # id assigned before outbox references it

    # Dispatch route: Buildbot is the sole orchestrator — persisted in
    # the same transaction as the execution (§7). The transport is
    # stamped so capacity-blocked QUEUED executions are redrivable —
    # their one-shot event is consumed even when no runner was
    # eligible at that tick.
    kind = "orchestrator-dispatch"
    if "dispatch_transport" not in snapshot:
        snapshot["dispatch_transport"] = "buildbot"
        execution.snapshot_json = json.dumps(snapshot)
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
    source_item = None
    if ex.suite is None:
        source_item = (db.session.get(DiscoveredItem, ex.source_item_id)
                       if ex.source_item_id else None)
        if source_item is None:
            raise DispatchError(
                "the repository item this execution ran is no longer "
                "present in discovery — re-run it from the Discovered "
                "page instead of retrying")
    rc = dict(snap.get("run_config") or {})
    # The snapshot stores normalized keys; create_execution consumes
    # the raw request keys — translate back so the retried execution
    # keeps its requested runner/runtime (and thus its transport).
    if rc.get("requested_runtime") and not rc.get("runtime_id"):
        rc["runtime_id"] = rc["requested_runtime"]
    if rc.get("requested_runner") and not rc.get("runner_id"):
        rc["runner_id"] = rc["requested_runner"]
    new_ex = create_execution(
        username, ex.suite, binding=binding, source_sha=ex.source_sha,
        source_item=source_item,
        plan_id=snap.get("plan_id") or "",
        run_config=rc,
        extra_snapshot={
            "plan_run_id": snap.get("plan_run_id") or "",
            "retry_of": ex.id,
            "attempt": int(snap.get("attempt") or 1) + 1,
        })
    record_audit(username, "execution.retry_queued",
                 f"{new_ex.id} (retry of {ex.id}, attempt "
                 f"{int(snap.get('attempt') or 1) + 1})")
    return new_ex


def _buildbot_configured() -> bool:
    """Buildbot is the sole orchestrator — 'configured' just means the
    loopback REST endpoint is known (installer/provisioner sets it)."""
    return bool(settings_mod.get("buildbot_endpoint"))


def _ssh_remote_cancel(ex: Execution) -> "bool | None":
    """Kill the detached remote process GROUP of an agentless-SSH run.

    The spec (host/user/key materialized at dispatch) lives in
    run-specs/<exid>.json; the remote pgid is <workdir>/run.pid —
    written by to_ssh_exec's setsid launch. Returns True only when
    remote death is verified (or nothing was ever launched), False on
    kill-attempted-but-unconfirmed, None when no SSH spec exists."""
    spec_path = buildbot_cfg.run_specs_dir() / f"{ex.id}.json"
    if not spec_path.is_file():
        return None
    try:
        spec = json.loads(spec_path.read_text())
        import base64
        import hashlib
        import shlex
        import socket as _sock
        import paramiko
        pkey = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey,
                    paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                pkey = cls.from_private_key_file(spec["key_file"])
                break
            except paramiko.SSHException:
                continue
        if pkey is None:
            return False
        wd = spec["remote_workdir"]
        qwd = shlex.quote(wd)
        transport = paramiko.Transport(_sock.create_connection(
            (spec["host"], int(spec.get("port") or 22)), timeout=15))
        try:
            transport.start_client(timeout=15)
            approved = spec.get("host_key_fingerprint") or ""
            if approved:
                rk = transport.get_remote_server_key()
                fp = ("SHA256:" + base64.b64encode(
                    hashlib.sha256(rk.asbytes()).digest()
                ).decode().rstrip("="))
                if fp != approved:
                    return False  # never authenticate to the wrong host
            transport.auth_publickey(spec["username"], pkey)

            def _run(cmd, timeout=15):
                chan = transport.open_session(timeout=timeout)
                chan.settimeout(timeout)
                chan.exec_command(cmd)
                out = chan.makefile("rb").read()
                err = chan.makefile_stderr("rb").read()
                return chan.recv_exit_status(), (
                    out + err).decode(errors="replace").strip()

            _, pid = _run(f"cat {qwd}/run.pid 2>/dev/null || true")
            if not pid.isdigit():
                return True  # never launched — nothing to kill
            _run(f"kill -TERM -{pid} 2>/dev/null; sleep 1; "
                 f"kill -KILL -{pid} 2>/dev/null; true", timeout=20)
            time.sleep(1)
            _, out = _run(f"kill -0 {pid} 2>/dev/null "
                          "&& echo alive || echo dead")
            return out == "dead"
        finally:
            transport.close()
    except Exception:
        return False


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
            transport = ex.snapshot().get("dispatch_transport")
            if transport == "buildbot":
                # One call covers queued AND running (verified 4.3.0):
                # a claimed buildrequest's cancel stops the build and
                # the worker kills the remote process tree.
                BuildbotAdapter.from_settings().cancel(_bb_brid(ex))
        except Exception:
            pass  # flag + lease expiry still bound the run
    # Agentless-SSH runs detach via setsid and the buildbot worker
    # SIGKILLs the dispatcher step (to_ssh_exec's SIGTERM handler never
    # runs) — the remote group survives unless the portal kills it
    # itself. CANCELED is only honest after remote death is confirmed.
    ssh_kill = _ssh_remote_cancel(ex)
    if ssh_kill is True:
        reason = (reason + " " if reason else "") \
            + "[ssh remote group terminated]"
    elif ssh_kill is False:
        reason = (reason + " " if reason else "") \
            + "[ssh remote kill UNCONFIRMED]"
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
    return capacity_limit(runner)


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


def _select_runner_runtime(ex: Execution):
    """Resolve the (runner, runtime) pair honoring an explicit
    run_config pick: an unavailable explicit pick is an error the
    caller must surface — never a silent retarget. Returns
    (runner, runtime, error): error non-empty -> fail the execution;
    runner None -> stay queued and retry next tick."""
    rc = ex.snapshot().get("run_config") or {}
    req_runner = rc.get("requested_runner") or ""
    req_runtime = rc.get("requested_runtime") or ""
    if req_runner:
        runner = db.session.get(Runner, req_runner)
        rn_allowed = (json.loads(runner.allowed_projects or "[]")
                      if runner else [])
        if (not runner or not runner.enabled or runner.draining
                or (rn_allowed and ex.project_id not in rn_allowed)):
            return None, None, (
                f"explicitly selected runner {req_runner[:12]}… is "
                "unavailable or out of scope for this project — no "
                "fallback to another target")
    elif req_runtime:
        # An explicitly selected runtime pins the runner — the
        # runtime's target IS the requested target (§2.6.0). Only fall
        # back to picking when no explicit selection exists.
        _rt = db.session.get(RuntimeConnection, req_runtime)
        runner = (_rt.runner if _rt and _rt.runner
                  and _rt.runner.enabled and not _rt.runner.draining
                  else None)
        if runner is None and _rt:
            return None, None, (
                f"runtime {req_runtime[:12]}… target is disabled or "
                "draining — no fallback")
        runner = runner or ex.runner or pick_runner(ex)
        if runner is None:
            return None, None, ""
    else:
        runner = ex.runner or pick_runner(ex)
        if runner is None:
            return None, None, ""
    if req_runtime:
        runtime = db.session.get(RuntimeConnection, req_runtime)
        rt_allowed = (json.loads(runtime.allowed_projects or "[]")
                      if runtime else [])
        if (not runtime or not runtime.enabled
                or runtime.runner_id != runner.id
                or (rt_allowed and ex.project_id not in rt_allowed)):
            return None, None, (
                f"explicitly selected runtime {req_runtime[:12]}… is "
                "unavailable or incompatible with the selected "
                "runner — no fallback")
    else:
        runtime = ex.runtime or pick_runtime(ex, runner)
        if runtime is None:
            return None, None, (
                "no enabled compatible runtime connection on runner "
                f"{runner.name!r} — register an existing KRE "
                "installation")
    return runner, runtime, ""


def run_execution(execution_id: str) -> None:
    """Run one execution through the full state machine. Any infra error is
    recorded; test failures do not stop collection (§7)."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
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
    runner, runtime, sel_err = _select_runner_runtime(ex)
    if sel_err:
        _fail(ex, "infrastructure", sel_err)
        return
    if runner is None:
        # Stay QUEUED — retry the next worker tick; do NOT mark
        # DISPATCHED (the assignment CAS requires QUEUED).
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
    except DispatchError as exc:  # configuration problem — name it
        _fail(ex, "config", str(exc))
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
    except SecretError as exc:
        raise DispatchError(
            f"runtime credential {runtime.credential_ref!r} is "
            f"configured but cannot be resolved: {exc}") from exc


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
    seen_hash = set()  # identical report collected via two layouts
                       # (e.g. SSH Reports/ + proj/src/Reports) must
                       # not double-count cases
    parsed_docs = []  # each artifact parsed once — reused for dedup
    for artifact in junit_artifacts:
        path = Path(ex.report_dir) / artifact.rel_path
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in seen_hash:
            continue
        try:
            parsed = parse_junit(path)
        except Exception:
            continue
        seen_hash.add(digest)
        parsed_docs.append(parsed)
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
    # Stamp dedup fingerprints so repository imports match this
    # live run — same canonicalisation the importer applies.
    try:
        from . import importer as _imp
        _uuid_f = Path(ex.report_dir or "") / "execution.uuid"
        if _uuid_f.is_file() and not ex.run_uuid:
            ex.run_uuid = _uuid_f.read_text().strip()[:64]
        if not ex.content_hash and cases:
            suite_keys, timestamps, totals = [], [], 0
            for parsed in parsed_docs:
                for s in parsed.suites:
                    suite_keys.append((s["id"] or s["name"],
                                       s["timestamp"]))
                    if s["timestamp"]:
                        timestamps.append(s["timestamp"])
                totals += parsed.producer_tests
            ex.content_hash = _imp.content_fingerprint(
                suite_keys, timestamps, [c[0] for c in cases], totals)
            if ex.suite_keys_json is None:
                ex.suite_keys_json = json.dumps(
                    sorted({tuple(k) for k in suite_keys}))
    except Exception:
        pass                        # fingerprinting never breaks normalize
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


def _bb_brid(ex: Execution) -> str:
    return ex.dispatch_id.split(":", 1)[1] \
        if ex.dispatch_id.startswith("bb:") else ex.dispatch_id


def _submit_buildbot(ex: Execution, runner: Runner,
                     runtime: RuntimeConnection) -> None:
    """Submit one execution to its per-target builder. The brid is
    persisted as ``bb:<brid>`` dispatch identity; a lost/ambiguous
    submission is reconciled by the execution-id PROPERTY before any
    resubmit — never blind-duplicated. Assignment is already durable
    (caller) before the master sees the buildrequest."""
    adapter = BuildbotAdapter.from_settings()
    if not ex.dispatch_id:
        # Reconcile-before-submit: a previous attempt may have created
        # the buildrequest without us reading the response — adopt an
        # existing live brid instead of creating a second run.
        live = [br for br in adapter.find_buildrequests(ex.id)
                if not br.get("complete")]
        if live:
            ex.dispatch_id = "bb:" + str(
                live[0]["buildrequestid"])
            db.session.commit()
            record_audit("system", "execution.buildbot_adopted",
                         f"{ex.id}: brid {ex.dispatch_id}")
            return
    source = _resolve_source(ex)
    buildbot_cfg.stage_source(ex, source)
    rc = ex.snapshot().get("run_config") or {}
    timeout_s = int(rc.get("timeout_seconds") or 3600)
    node_kind = getattr(runtime, "runtime_kind", "") or "kre"
    if node_kind in nodefw.KINDS:
        ok_b, browser_val = nodefw.validate_browser(
            node_kind, rc.get("browser", ""))
        if not ok_b:
            _fail(ex, "config", browser_val)
            return
        node_args = nodefw.build_args(node_kind, browser_val)
        node_env = nodefw.junit_env(node_kind)
        win = (runner.os_arch or "").lower().startswith("win")
        # npm sits next to npx — an explicit executable_path resolves
        # the sibling, otherwise PATH does.
        npx_path = runtime.executable_path or (
            "npx.cmd" if win else "npx")
        npm = str(Path(npx_path).with_name("npm.cmd" if win
                                           else "npm")) \
            if Path(npx_path).parent != Path(".") else (
                "npm.cmd" if win else "npm")
        deps_argv = [npm, "ci", "--no-audit", "--no-fund"]
        if runner.target_type == "ssh":
            buildbot_cfg.materialize_ssh_key(runner)
            extra = {"exec_subdir": "src", "env": node_env,
                     "deps_argv": deps_argv,
                     "report_rel": "proj/src/Reports"}
            npx_dir = str(Path(npx_path).parent)
            if Path(npx_path).is_absolute() and npx_dir != ".":
                extra["path_prefix"] = npx_dir
            spec_file = buildbot_cfg.write_ssh_run_spec(
                ex, runner, runtime, node_args, extra=extra)
            import sys as _sys
            spec = buildbot_cfg.ssh_exec_spec(
                ex, python=_sys.executable,
                dispatcher_script=str(
                    Path(__file__).resolve().parent.parent / "scripts"
                    / "to_ssh_exec.py"),
                spec_file=str(spec_file))
        else:
            spec = buildbot_cfg.node_exec_spec(
                ex, npx_path, node_args, env=node_env,
                deps_argv=deps_argv)
            buildbot_cfg.run_specs_dir().mkdir(parents=True,
                                               exist_ok=True)
            (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
             ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner)
        brid = adapter.submit(
            builder, ex.id,
            {"exec_spec": BuildbotAdapter.encode_props(spec),
             "exec_timeout": str(timeout_s)})
        ex.dispatch_id = "bb:" + str(brid)
        db.session.commit()
        record_audit("system", "execution.buildbot_dispatched",
                     f"{ex.id}: {builder} ({node_kind}) brid {brid}")
        return
    if node_kind == robotfw.KIND:
        # python -m robot under the runtime's configured interpreter —
        # no browser concept (browsers belong to the project and its
        # libraries), no deps step (deps live in the configured env).
        robot_args = robotfw.build_args(
            suite_path=(rc.get("suite_path") or "").strip(),
            test=(rc.get("robot_test") or "").strip(),
            suite=(rc.get("robot_suite") or "").strip(),
            include=_tag_list(rc.get("robot_include")),
            exclude=_tag_list(rc.get("robot_exclude")))
        win = (runner.os_arch or "").lower().startswith("win")
        py = (runtime.executable_path or "").strip() \
            or ("python.exe" if win else "python3")
        if runner.target_type == "ssh":
            buildbot_cfg.materialize_ssh_key(runner)
            extra = {"exec_subdir": "src",
                     "report_rel": "proj/src/Reports"}
            py_dir = str(Path(py).parent)
            if Path(py).is_absolute() and py_dir != ".":
                extra["path_prefix"] = py_dir
            spec_file = buildbot_cfg.write_ssh_run_spec(
                ex, runner, runtime, robot_args, executable=py,
                extra=extra)
            import sys as _sys
            spec = buildbot_cfg.ssh_exec_spec(
                ex, python=_sys.executable,
                dispatcher_script=str(
                    Path(__file__).resolve().parent.parent / "scripts"
                    / "to_ssh_exec.py"),
                spec_file=str(spec_file))
        else:
            spec = buildbot_cfg.node_exec_spec(ex, py, robot_args)
            buildbot_cfg.run_specs_dir().mkdir(parents=True,
                                               exist_ok=True)
            (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
             ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner)
        brid = adapter.submit(
            builder, ex.id,
            {"exec_spec": BuildbotAdapter.encode_props(spec),
             "exec_timeout": str(timeout_s)})
        ex.dispatch_id = "bb:" + str(brid)
        db.session.commit()
        record_audit("system", "execution.buildbot_dispatched",
                     f"{ex.id}: {builder} (robot) brid {brid}")
        return
    flags, dropped = kre_adapter.gated_flags(
        runtime.detected_version, rc.get("kre_options") or {})
    if dropped and not rc.get("kre_options_allow_drop"):
        _fail(ex, "infrastructure",
              f"version-gated options rejected for detected version "
              f"{runtime.detected_version or 'unknown'}: "
              + ", ".join(dropped))
        return
    ok_b, browser_val = kre_adapter.validate_browser(
        rc.get("browser", ""), "linux")
    if not ok_b:
        _fail(ex, "config", browser_val)
        return

    if runner.target_type == "ssh":
        cred_file = runtime.credential_file or ""
        if runtime.credential_ref and not cred_file:
            _fail(ex, "config",
                  "ssh target: runtime credential_ref is portal-side "
                  "only — configure a credential_file path on the "
                  "remote host instead")
            return
        buildbot_cfg.materialize_ssh_key(runner)
        remote = runner.ssh_workdir.rstrip("/") + "/" + ex.id
        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            # source.tar holds a top-level src/ — the remote project
            # root after untar is <workdir>/proj/src.
            project_path=remote + "/proj/src",
            suite_path=_snapshot_suite_path(ex),
            report_dir=remote + "/Reports",
            profile=rc.get("profile", "default"),
            browser=browser_val or "Chrome (headless)",
            remote_webdriver_url=rc.get("remote_webdriver_url", ""),
            remote_webdriver_type=rc.get("remote_webdriver_type", ""),
            collection=bool(rc.get("collection")),
            extra=flags)
        spec_file = buildbot_cfg.write_ssh_run_spec(
            ex, runner, runtime, args[1:], cred_file)
        import sys as _sys
        spec = buildbot_cfg.ssh_exec_spec(
            ex,
            # The dispatcher runs on the controller host — this same
            # interpreter (payload venv in packaged installs) has
            # paramiko and the dispatcher script's deps.
            python=_sys.executable,
            dispatcher_script=str(
                Path(__file__).resolve().parent.parent / "scripts"
                / "to_ssh_exec.py"),
            spec_file=str(spec_file))
        builder = buildbot_cfg.builder_for(runner)
    else:
        cred_file = runtime.credential_file or ""
        if not cred_file and runtime.credential_ref:
            if runner.target_type == "local":
                # Controller-host worker: portal resolves the secret to
                # a per-run 0600 file the wrapper reads — never argv.
                from .secrets_store import read_secret
                cred_file = buildbot_cfg.write_run_credential(
                    ex.id, read_secret(
                        current_app._get_current_object(),
                        runtime.credential_ref))
            else:
                _fail(ex, "config",
                      "remote worker: runtime credential_ref is "
                      "portal-side only — configure a credential_file "
                      "on the worker host instead")
                return
        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            project_path=".",
            suite_path=_snapshot_suite_path(ex),
            report_dir="Reports",
            profile=rc.get("profile", "default"),
            browser=browser_val or "Chrome (headless)",
            remote_webdriver_url=rc.get("remote_webdriver_url", ""),
            remote_webdriver_type=rc.get("remote_webdriver_type", ""),
            collection=bool(rc.get("collection")),
            extra=flags)
        spec = buildbot_cfg.kre_exec_spec(
            ex, runtime, args[1:], credential_file=cred_file)
        # Persist the rendered spec — collection re-renders it as the
        # kre-invocation.log evidence record (same contract the local
        # path had: the exact argv, never secret values).
        buildbot_cfg.run_specs_dir().mkdir(parents=True, exist_ok=True)
        (buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
         ).write_text(json.dumps(spec))
        builder = buildbot_cfg.builder_for(runner)

    brid = adapter.submit(
        builder, ex.id,
        {"exec_spec": BuildbotAdapter.encode_props(spec),
         "exec_timeout": str(timeout_s)})
    ex.dispatch_id = "bb:" + str(brid)
    db.session.commit()
    record_audit("system", "execution.buildbot_dispatched",
                 f"{ex.id}: {builder} brid {brid}")


def _buildbot_assign_and_submit(ex: Execution) -> None:
    """Buildbot dispatch (docs/orchestration-replacement.md): same
    ordering contract — assignment commits BEFORE the
    buildrequest exists so a claimed build's first poll sees a durable
    DISPATCHED row; explicit picks are honored or visibly rejected; a
    lost capacity CAS or unreachable master leaves the row QUEUED for
    the redrive (which reconciles by property before resubmitting)."""
    # Dispatch boundary: only still-pending work may enter — an
    # execution cancelled while its outbox event waited is skipped and
    # audited, never launched (same contract as run_execution's gate).
    if ex.state not in (ExecutionState.QUEUED,):
        record_audit("system", "execution.dispatch_skipped",
                     f"{ex.id}: state={ex.state.value}")
        return
    runner, runtime, sel_err = _select_runner_runtime(ex)
    if sel_err:
        _fail(ex, "infrastructure", sel_err)
        return
    if runner is None:
        return
    if not _assign_runner(ex, runner, runtime):
        return
    try:
        _submit_buildbot(ex, runner, runtime)
    except Exception as exc:
        import requests as _req
        if isinstance(exc, (_req.ConnectionError, _req.Timeout)):
            # Connect-level failure — the request provably never
            # reached the master (or the reconcile query will adopt
            # whatever did): release the slot, stay QUEUED, retry.
            ex.state = ExecutionState.QUEUED
            ex.runner_id = None
            ex.runtime_id = None
            ex.lease_expires_at = None
            ex.error_text = (
                ex.error_text + " | buildbot master unreachable; "
                "held QUEUED for redrive")[:2000]
            db.session.commit()
            return
        _fail(ex, "infrastructure",
              f"Buildbot submission failed — reconcile before retry: "
              f"{str(exc)[:300]}")
        return
    db.session.refresh(ex)
    if ex.state == ExecutionState.CANCEL_REQUESTED:
        # Cancel landed between assignment and submit — issue the
        # buildrequest cancel now (brid exists, request_cancel saw none).
        try:
            BuildbotAdapter.from_settings().cancel(_bb_brid(ex))
        except Exception:
            pass


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
    if event.kind in ("local-dispatch",
                      "orchestrator-dispatch"):
        # Buildbot is the sole orchestrator; legacy in-flight event
        # kinds route to the same assign->submit path.
        ex = db.session.get(Execution, payload["execution_id"])
        if ex is not None:
            _buildbot_assign_and_submit(ex)
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
                            d_item.project_id
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
                    BuildbotAdapter.from_settings().cancel(
                        _bb_brid(sib))
                except Exception:
                    pass
            record_audit("system", "plan.stop_on_failure_cancel", sib.id)
            cancelled += 1
    if cancelled:
        db.session.commit()
    return cancelled


def _heartbeat_local_runners() -> int:
    """The flask worker IS the execution driver for every
    connection_mode=local runner on this instance — its own liveness
    is their availability signal (§6). Stamped while the loop lives;
    when it stops, the existing freshness window marks them ineligible.
    Heartbeat certifies driver liveness only — never KRE/credential
    availability, which separate checks own."""
    runners = Runner.query.filter_by(enabled=True,
                                     connection_mode="local").all()
    for r in runners:
        r.heartbeat_at = utcnow()
        r.health = "ok"
    if runners:
        db.session.commit()
    return len(runners)


def _redrive_queued_remote(limit: int = 10) -> int:
    """Capacity-blocked executions: their dispatch outbox event is
    consumed once — a lost capacity CAS or a no-runner tick must not
    strand them QUEUED forever. Each tick revisits QUEUED executions
    with no dispatch_id: assign->submit runs again only while
    dispatch_id is empty — assignment precedes submit, so QUEUED + no
    dispatch_id proves no run was ever started; _submit_buildbot
    additionally reconciles by execution-id property before forcing a
    new buildrequest."""
    n = 0
    for ex in Execution.query.filter(
            Execution.state == ExecutionState.QUEUED).limit(limit):
        transport = ex.snapshot().get("dispatch_transport")
        if transport == "buildbot" and not ex.dispatch_id:
            _buildbot_assign_and_submit(ex)
            if ex.state != ExecutionState.QUEUED:
                n += 1
    return n


def worker_once() -> int:
    # Orchestrator reconciliation (§7) runs BEFORE the lease sweep: an
    # aborted run with a pending cancel request must resolve to
    # CANCELED from the coordinator's authoritative status — not to
    # UNKNOWN merely because the lease elapsed first in the same tick.
    # A finished run with no portal result is ambiguous -> UNKNOWN,
    # never a blind re-dispatch.
    n = 0
    if _buildbot_configured():
        try:
            n += reconcile_buildbot_runs()
        except Exception:
            pass  # orchestrator unreachable — retry next cycle
    n += (reconcile_stale() + fire_due_schedules() + process_outbox()
          + enforce_plan_policies()
          + _heartbeat_local_runners() + _redrive_queued_remote())
    return n


def worker_loop(interval: float = 2.0) -> None:
    while True:
        try:
            worker_once()
        finally:
            # Release the session/transaction every cycle — a left-open
            # transaction holds the SQLite write lock and starves the
            # web app's request-time provisioning writes.
            db.session.remove()
        time.sleep(interval)


# ---------- Buildbot orchestration (docs/orchestration-replacement.md) -

def _collect_buildbot(adapter: BuildbotAdapter, ex: Execution,
                      results_code: int) -> None:
    """Terminal buildrequest -> materialize master-side uploaded
    artifacts into the portal collection dir and run the SAME
    collect/normalize/finalize pipeline — parallel to _collect_remote.
    Artifacts land at <buildbot_home>/artifacts/<exec_id> via the
    builder's DirectoryUpload step; the step log comes over the
    localhost API."""
    attempt_dir = (Path(current_app.instance_path)
                   / "remote-runs" / ex.id)
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"
    # Capture the cancel intent BEFORE the collection pipeline
    # transitions the row through COLLECTING/NORMALIZING/FINALIZING —
    # checking ex.state at the end would never see CANCEL_REQUESTED.
    cancel_requested = (ex.state == ExecutionState.CANCEL_REQUESTED
                        or bool(ex.cancel_reason))
    rt0 = db.session.get(RuntimeConnection, ex.runtime_id)
    try:
        creds = _redact_values(rt0) if rt0 else []
    except DispatchError:
        creds = []

    def _scrub(data: bytes) -> bytes:
        for c in creds:
            data = data.replace(c.encode("utf-8", "replace"),
                                b"***REDACTED***")
        return data

    import shutil as _sh
    src_dir = buildbot_cfg.artifacts_dir() / ex.id
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        if src_dir.is_dir():
            for item in src_dir.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src_dir)
                    dest = report_dir.parent / rel if str(
                        rel).startswith("Reports") else report_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(_scrub(item.read_bytes()))
    except Exception as exc:
        _fail(ex, "infrastructure",
              f"buildbot run {ex.dispatch_id} ended "
              f"(results={results_code}) but artifact collection "
              f"failed: {exc}")
        return
    try:
        spec_path = buildbot_cfg.run_specs_dir() / f"{ex.id}.spec.json"
        if spec_path.is_file():
            spec = json.loads(spec_path.read_text())
            if spec.get("executable"):
                argv = [spec["executable"], *spec.get("args", [])]
                if spec.get("credential_wrapper") \
                        and spec.get("credential_file"):
                    argv = [spec["credential_wrapper"],
                            spec["credential_file"], *argv]
                import shlex as _shlex
                (log_dir / "kre-invocation.log").write_text(
                    " ".join(_shlex.quote(str(a)) for a in argv) + "\n",
                    encoding="utf-8")
    except Exception:
        pass  # invocation record is best-effort evidence
    try:
        builds = adapter.builds_for_request(_bb_brid(ex))
        if builds:
            (log_dir / "kre-remote-run.log").write_bytes(_scrub(
                adapter.run_log(builds[0]["buildid"])
                .encode("utf-8", "replace")))
    except Exception:
        pass  # run log is best-effort evidence
    ex.workspace_path = str(attempt_dir)
    ex.report_dir = str(report_dir)
    ex.exit_status = 0 if results_code == 0 else 1
    ex.finished_at = utcnow()
    _transition(ex, ExecutionState.COLLECTING)
    _collect(ex, report_dir, log_dir)
    _transition(ex, ExecutionState.NORMALIZING)
    _normalize(ex)
    _transition(ex, ExecutionState.FINALIZING)
    _sync_git(ex)
    buildbot_cfg.clear_staging(ex.id)
    has_results = bool(TestResult.query.filter_by(
        execution_id=ex.id).first())
    if results_code == 6 or (cancel_requested
                             and not (results_code == 0 or has_results)):
        # CANCELLED — or any non-success result after a cancel request:
        # buildrequest cancel may surface as RETRY/FAILURE depending on
        # where the worker was in the step, but the user intent was
        # recorded and must govern the terminal state.
        if cancel_requested:
            if "UNCONFIRMED" in (ex.cancel_reason or ""):
                ex.state = ExecutionState.FAILED
                ex.error_kind = "infrastructure"
                ex.error_text = (
                    f"cancellation requested but remote process-tree "
                    f"death was not confirmed (buildbot run "
                    f"{ex.dispatch_id} ended results={results_code}) — "
                    "manual verification required")
            else:
                ex.state = ExecutionState.CANCELED
            ex.outcome = Outcome.INCOMPLETE
        else:
            # Unrequested abort — worker loss/external stop are
            # indistinguishable to the portal; honest uncertainty,
            # never a mislabeled user cancellation.
            ex.state = ExecutionState.FAILED
            ex.error_kind = "infrastructure"
            ex.error_text = (
                f"buildbot run {ex.dispatch_id} ended CANCELLED "
                "without a recorded cancellation request — cause "
                "undetermined; any late evidence was collected")
            ex.outcome = Outcome.INCOMPLETE
            record_audit("system",
                         "execution.buildbot_aborted_unrequested", ex.id)
    elif results_code == 0 or has_results:
        # A failed build with real junit is failed TESTS — the
        # normalized outcome carries the verdict, not the step status.
        ex.state = ExecutionState.COMPLETED
    else:
        ex.state = ExecutionState.FAILED
        # EXCEPTION (4) = the step never ran the test binary — an
        # infrastructure/config failure (missing executable, worker
        # error), not a test verdict. The step log carries the cause.
        if results_code == 4:
            ex.error_kind = "infrastructure"
            tail = ""
            try:
                builds = adapter.builds_for_request(_bb_brid(ex))
                if builds:
                    tail = adapter.run_log(
                        builds[0]["buildid"]).strip()[-300:]
            except Exception:
                pass
            ex.error_text = (
                f"buildbot run {ex.dispatch_id} ended EXCEPTION — the "
                f"test executable did not run"
                + (f": {tail}" if tail else ""))
        else:
            ex.error_kind = "execution"
            detail = ""
            if rt0 is not None and (rt0.runtime_kind or "") \
                    == robotfw.KIND:
                # Robot writes xunit only on a valid execution —
                # classify the console log so invalid data / missing
                # libraries / interruptions are named, not lumped
                # into "no result data".
                label = ""
                for cand in (report_dir / "run.out",
                             log_dir / "kre-remote-run.log",
                             report_dir / "Reports" / "output.xml"):
                    try:
                        label = robotfw.classify_log(
                            cand.read_text(errors="replace"))
                    except OSError:
                        continue
                    if label:
                        break
                # SSH runs drop an exit_code artifact — the recorded
                # Robot rc sharpens the classification (e.g. 251
                # informational, 253 user stop, 252 invalid data).
                if not label:
                    for cand in (report_dir / "exit_code",
                                 report_dir / "Reports" / "exit_code"):
                        try:
                            rc = int(cand.read_text().strip())
                        except (OSError, ValueError):
                            continue
                        sev, label = robotfw.classify_exit(rc)
                        label = f"exit {rc}: {label}"
                        if sev in ("config", "interrupted", "internal"):
                            ex.error_kind = (
                                "execution" if sev == "interrupted"
                                else "infrastructure")
                        break
                detail = f" ({label})" if label else ""
            ex.error_text = (f"buildbot run {ex.dispatch_id} ended "
                             f"results={results_code} with no result "
                             f"data{detail}")
    db.session.commit()


def reconcile_buildbot_runs() -> int:
    """Poll DISPATCHED buildbot executions (parallel to
    the orchestrator reconcile): finished-without-result -> UNKNOWN is NOT
    reachable here — a completed buildrequest always maps to a real
    terminal transition; unrequested CANCELLED stays honest. Live
    buildrequests renew the lease."""
    adapter = None
    fixed = 0
    for ex in Execution.query.filter(
            Execution.state.in_([ExecutionState.DISPATCHED,
                                 ExecutionState.CANCEL_REQUESTED])).all():
        if ex.snapshot().get("dispatch_transport") != "buildbot" \
                or not ex.dispatch_id:
            continue
        adapter = adapter or BuildbotAdapter.from_settings()
        try:
            br = adapter.buildrequest(_bb_brid(ex))
        except Exception:
            continue    # transient — lease expiry bounds the ambiguity
        if not br:
            continue
        if not br.get("complete"):
            ex.lease_expires_at = utcnow() + timedelta(
                seconds=LEASE_SECONDS)
            db.session.commit()
            continue
        _collect_buildbot(adapter, ex, int(br.get("results") or 0))
        fixed += 1
    # Runner liveness from the worker registry: a registered-but-
    # unconnected worker is distinguishable from an unreachable SSH
    # host (different mechanism, different UI state).
    if adapter is not None:
        try:
            online = {w.get("name") for w in adapter.workers()
                      if w.get("connected_to")}
            for r in Runner.query.filter(
                    Runner.target_type == "buildbot-worker",
                    Runner.enabled.is_(True)).all():
                live = r.bb_worker_name in online
                r.health = "ok" if live else "unreachable"
                if live:
                    r.heartbeat_at = utcnow()
            db.session.commit()
        except Exception:
            pass
    return fixed
