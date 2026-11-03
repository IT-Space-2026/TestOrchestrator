"""Execution pipeline: dispatch, state machine, local runner (§4–§8).

Workflow: prepare -> execute -> collect -> normalize -> publish -> finalize.
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
from .adapters.reportportal import (NotConfigured as RpNotConfigured,
                                    ReportPortalAdapter)
from .execution import (Artifact, Execution, ExecutionState, GitBinding,
                        OutboxEvent, Outcome, Runner, RuntimeConnection,
                        Suite, SyncState, TestResult, utcnow)
from .extensions import db
from .models import record_audit
from .results import aggregate_outcome, parse_junit
from . import results as results_mod

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15


class DispatchError(RuntimeError):
    pass


# ---------- creation / snapshot ----------

def create_execution(username: str, suite: Suite, binding: GitBinding = None,
                     source_sha: str = "") -> Execution:
    """Create an immutable execution snapshot (§4, A07). Later suite edits
    create new suite versions; this row's snapshot never changes."""

    snapshot = {
        "suite_id": suite.id,
        "suite_name": suite.name,
        "suite_version": suite.version,
        "suite_hash": suite.content_hash,
        "entries": [
            {
                "order": e.order,
                "case_ref": e.case_ref,
                "enabled": e.enabled,
                "config": json.loads(e.config_json or "{}"),
            }
            for e in suite.entries
        ],
        "binding_id": binding.id if binding else None,
        "source_sha": source_sha,
        "created_by": username,
    }
    execution = Execution(
        project_id=suite.project_id,
        suite_id=suite.id,
        binding_id=binding.id if binding else None,
        snapshot_json=json.dumps(snapshot),
        source_sha=source_sha,
        created_by=username,
    )
    db.session.add(execution)
    db.session.flush()  # id assigned before outbox references it

    # Dispatch route: Dagu when configured, else the local runner outbox.
    # Persisted in the same transaction as the execution (§7).
    kind = "dagu-dispatch" if _dagu_configured() else "local-dispatch"
    db.session.add(OutboxEvent(
        kind=kind, payload_json=json.dumps({"execution_id": execution.id})))
    db.session.commit()
    return execution


def _dagu_configured() -> bool:
    return bool(settings_mod.get("dagu_endpoint"))


def request_cancel(execution_id: str, username: str, reason: str = "") -> bool:
    """Cancel targets the actual process tree (§6); stays CANCEL_REQUESTED
    until termination is acknowledged — uncertainty is surfaced."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        return False
    if ex.state in (ExecutionState.COMPLETED, ExecutionState.FAILED,
                    ExecutionState.CANCELED):
        return False
    ex.state = ExecutionState.CANCEL_REQUESTED
    ex.cancel_reason = reason[:256]
    db.session.commit()
    record_audit(username, "execution.cancel_requested", execution_id)
    return True


# ---------- runner selection (§6) ----------

def effective_slots(runner: Runner) -> int:
    """Free capacity = max_slots - currently leased running executions."""
    active = Execution.query.filter(
        Execution.runner_id == runner.id,
        Execution.state.in_([
            ExecutionState.DISPATCHED, ExecutionState.PREPARING,
            ExecutionState.RUNNING, ExecutionState.COLLECTING,
            ExecutionState.NORMALIZING, ExecutionState.CANCEL_REQUESTED,
        ]),
    ).count()
    return max(0, runner.max_slots - active)


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


def run_execution(execution_id: str) -> None:
    """Run one execution through the full state machine. Any infra error is
    recorded; test failures do not stop collection (§7)."""
    ex = db.session.get(Execution, execution_id)
    if ex is None:
        return
    runner = ex.runner or pick_runner(ex)
    if runner is None:
        # Stay queued/dispatched — no free compatible runner right now.
        ex.state = ExecutionState.DISPATCHED
        db.session.commit()
        return
    runtime = ex.runtime or pick_runtime(ex, runner)
    if runtime is None:
        _fail(ex, "infrastructure",
              "no enabled compatible runtime connection on runner "
              f"{runner.name!r} — register an existing KRE installation")
        return
    ex.runner_id = runner.id
    ex.runtime_id = runtime.id
    ex.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
    db.session.commit()

    if runner.connection_mode != "local":
        # Remote transport (§6): the assigned agent picks the task up via
        # /api/agent/tasks; the portal holds the lease, heartbeats renew
        # it, expiry reconciles to UNKNOWN — never silent re-dispatch.
        if not runner.agent_token_hash:
            _fail(ex, "infrastructure",
                  f"remote runner {runner.name!r} has no issued agent token")
            return
        ex.state = ExecutionState.DISPATCHED
        db.session.commit()
        return

    workspace_root = Path(
        runtime.workspace_root or runner.workspace_root
        or Path(current_app.instance_path) / "workspaces"
    )
    attempt_dir = workspace_root / execution_id
    report_dir = attempt_dir / "Reports"
    log_dir = attempt_dir / "logs"

    try:
        _transition(ex, ExecutionState.PREPARING)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        source_dir = _resolve_source(ex)
        project_dir = kre_adapter.prepare_workspace(
            source_dir, attempt_dir, ex.source_sha
        )
        ex.workspace_path = str(attempt_dir)
        ex.report_dir = str(report_dir)
        ex.started_at = utcnow()

        args = kre_adapter.build_args(
            executable=runtime.executable_path,
            project_path=str(project_dir),
            suite_path=_snapshot_suite_path(ex),
            report_dir=str(report_dir),
        )

        _transition(ex, ExecutionState.RUNNING)
        result = kre_adapter.run_invocation(
            args, log_dir, cancel_event=_DbCancelEvent(ex.id),
        )
        ex.exit_status = result.exit_status
        ex.finished_at = utcnow()

        # Collection ALWAYS runs — including after failures (§5, §7).
        _transition(ex, ExecutionState.COLLECTING)
        _collect(ex, report_dir, log_dir, result)

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

        # Evidence publish/export still runs for canceled executions —
        # partial evidence is real. Only the terminal state differs.
        _transition(ex, ExecutionState.PUBLISHING)
        _publish(ex)

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
    entries = [e for e in ex.snapshot().get("entries", []) if e.get("enabled")]
    first = entries[0]["case_ref"] if entries else ""
    return first or "Test Suites/suite"


def _collect(ex: Execution, report_dir: Path, log_dir: Path, result) -> None:
    """Evidence collection: JUnit discovery + manifest with checksums;
    every file is registered, unmapped files stay at run level (§9)."""
    junit_files = set(kre_adapter.discover_reports(report_dir))
    manifest = results_mod.build_evidence_manifest(report_dir, junit_files)
    # Runner logs are evidence too.
    for log_path in (Path(result.stdout_path), Path(result.stderr_path)):
        if log_path.exists():
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


def _publish(ex: Execution) -> None:
    """ReportPortal publication — local canonical results are authoritative;
    an outage leaves evidence intact and never reruns tests (§9, A03)."""
    try:
        adapter = ReportPortalAdapter.from_settings()
        adapter.publish_execution(ex)
        ex.rp_state = SyncState.DONE
    except RpNotConfigured:
        ex.rp_state = SyncState.NOT_CONFIGURED
    except Exception as exc:
        # If any remote side-effects may already exist (launch/item IDs
        # persisted earlier), the outcome is AMBIGUOUS, not a clean FAILED
        # — blind retry could duplicate accepted remote work (§9).
        partial = bool(ex.rp_launch_id) or any(
            r.rp_remote_id for r in ex.results) or any(
            a.rp_state == SyncState.DONE for a in ex.artifacts)
        ex.rp_state = SyncState.AMBIGUOUS if partial else SyncState.FAILED
        ex.error_text = (ex.error_text + f" | publish: {exc}")[:2000]
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
    if event.kind in ("local-dispatch", "dagu-dispatch"):
        if event.kind == "dagu-dispatch":
            try:
                dispatch_id = DaguAdapter.from_settings().submit(payload)
                ex = db.session.get(Execution, payload["execution_id"])
                if ex:
                    ex.dispatch_id = dispatch_id
                    db.session.commit()
            except DaguNotConfigured:
                # Endpoint removed after creation — fall back to local run.
                pass
        run_execution(payload["execution_id"])
    elif event.kind == "git-sync":
        from . import gitsync
        gitsync.sync_binding(payload["binding_id"])
    elif event.kind == "rp-publish":
        ex = db.session.get(Execution, payload["execution_id"])
        if ex:
            _publish(ex)
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
        ex.error_text = (
            ex.error_text + " | lease expired; state uncertain "
            "(reconciled, not re-run)").strip(" |")[:2000]
        db.session.commit()
    return len(stale)


def worker_once() -> int:
    return reconcile_stale() + process_outbox()


def worker_loop(interval: float = 2.0) -> None:
    while True:
        worker_once()
        time.sleep(interval)
